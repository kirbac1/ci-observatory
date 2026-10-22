"""Unit tests for the parts that turn GitHub data into numbers: no network, no gh."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ci_observatory import report
from ci_observatory.actions import discover
from ci_observatory.cli import shareable
from ci_observatory.logs import (
    AuditJob,
    TestUnits,
    analyse,
    base_job,
    matrix_variant,
    redundancy,
    sanitise,
)
from ci_observatory.signals import RepoSignals, ai_mentions

# ------------------------------------------------------------------------------------------------- AI attribution


@pytest.mark.parametrize(
    ("text", "agents", "models"),
    [
        ("Co-authored-by: Cursor <cursoragent@cursor.com>", {"Cursor"}, set()),
        ("Assisted-by: Claude Opus 5.5", {"Agent not named"}, {"Claude Opus 5.5"}),
        ("Assisted-by: Composer", {"Cursor"}, {"Cursor Composer"}),
        ("Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>", {"Claude Code"}, {"Claude Fable 5.1"}),
        ("Co-authored-by: Copilot Autofix powered by AI <x@users.noreply.github.com>", {"Copilot Autofix"}, set()),
        ("🤖 Generated with [Claude Code](https://claude.com/claude-code)", {"Claude Code"}, set()),
        # A person called Claude is a person.
        ("Co-authored-by: Claude Monet <monet@example.com>", set(), set()),
        ("Co-authored-by: Jane Doe <jane@example.com>", set(), set()),
    ],
)
def test_ai_mentions(text: str, agents: set[str], models: set[str]) -> None:
    assert ai_mentions(text) == (agents, models)


# ------------------------------------------------------------------------------------------------- log parsing


def stamp(base: datetime, seconds: float) -> str:
    return (base + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%f0Z")


def fake_job() -> tuple[str, dict[str, Any]]:
    base = datetime(2026, 10, 17, 13, 21, 54, tzinfo=UTC)
    lines = [
        (0.0, "Current runner version: '2.337.0'"),
        (0.1, "##[group]Run make test"),
        (0.11, "make test"),
        (0.12, "shell: /usr/bin/bash -e {0}"),
        (0.13, "##[endgroup]"),
        (1.0, "============================= test session starts =============================="),
        (1.1, "collected 3 items"),
        (2.0, "tests/test_a.py::test_one PASSED [ 33%]"),
        (2.1, "tests/test_a.py::test_two FAILED [ 66%]"),
        (2.2, "tests/test_a.py::test_three SKIPPED [100%]"),
        (2.3, "=========================== short test summary info ============================"),
        (2.4, "FAILED tests/test_a.py::test_two - AssertionError: assert 1 == 2"),
        (2.5, "==================== 1 failed, 1 passed, 1 skipped in 1.20s ===================="),
        (2.6, "##[error]Process completed with exit code 1."),
        (2.7, "Post job cleanup."),
        (2.8, "Cleaning up orphan processes"),
    ]
    raw = "\n".join(f"{stamp(base, t)} {text}" for t, text in lines)

    def at(seconds: int) -> str:
        return (base + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")

    steps = [
        {"name": "Set up job", "number": 1, "conclusion": "success", "started_at": at(0), "completed_at": at(0)},
        {"name": "Run tests", "number": 2, "conclusion": "failure", "started_at": at(0), "completed_at": at(2)},
        {"name": "Complete job", "number": 3, "conclusion": "success", "started_at": at(2), "completed_at": at(2)},
    ]
    job = {"id": 2, "name": "test", "conclusion": "failure", "created_at": at(0), "started_at": at(0), "completed_at": at(3)}
    meta = {"repo": "o/r", "run": {"id": 1, "workflow": "CI", "event": "pull_request"}, "job": job, "steps": steps}
    return raw, {**meta, "reason": "failed"}


def test_failed_job_is_classified_from_its_own_output() -> None:
    raw, meta = fake_job()
    parsed = analyse(raw, meta)
    assert parsed is not None
    assert parsed["failure"]["class"] == "test"
    assert parsed["failure"]["step"] == "Run tests"
    assert "test_two" in parsed["failure"]["signature"]
    assert parsed["tests"] == {"kind": "pytest", "total": 3, "failed": 1, "skipped": 1, "passed": 1}
    assert parsed["test_units"]["units"] == 3
    assert parsed["errors"] == 1


def test_sanitise_keeps_code_and_drops_people() -> None:
    assert sanitise("Author: Jane Doe") is None
    assert sanitise("git config user.email jane@example.com") is None
    assert sanitise("Unable to resolve action actions/checkout@v7.0.1") == "Unable to resolve action actions/checkout@v7.0.1"
    assert sanitise("mail ops@example.com now") == "mail <email> now"
    assert sanitise("token ghp_abcdefghijklmnopqrstuvwxyz0123456789") == "token <token>"
    assert sanitise("API rate limit exceeded for 51.57.84.136.") == "API rate limit exceeded for <ip>."
    assert sanitise("/home/runner/work/api/api/src/x.py") == "./src/x.py"
    assert sanitise("HEAD is now at bbd84276 Fix the thing Jane asked for") == "HEAD is now at bbd84276"


# ------------------------------------------------------------------------------------------------- test redundancy


def test_matrix_legs_share_a_base_name() -> None:
    assert base_job("test (3.13, ubuntu-latest)") == base_job("test (3.12, macos-latest)")
    assert base_job("JUnit tests Java 17") == base_job("JUnit tests Java 25")
    assert base_job("E2E tests for S3") != base_job("E2E tests for Gcs")
    assert matrix_variant("JUnit tests Java 17") == "17"
    assert matrix_variant("test (3.13, ubuntu-latest)") == "3.13, ubuntu-latest"


def units(*names: str, tests: int = 1) -> TestUnits:
    found = TestUnits()
    for name in names:
        found.add("pytest", name, tests=tests)
    return found


def test_redundancy_buckets_add_up() -> None:
    suite = ("a::one", "a::two", "a::three")
    jobs = [
        AuditJob("CI", "test (3.12)", "pull_request", 60, "success", units(*suite)),
        AuditJob("CI", "test (3.13)", "pull_request", 60, "success", units(*suite)),  # matrix leg
        AuditJob("CI", "test (3.12)", "push", 60, "success", units(*suite)),  # same commit, push event
        AuditJob("CodeQL", "Analyze (python)", "pull_request", 90, "success", units(*suite)),  # scan
    ]
    found = redundancy("o/r", {"sha": "abcdef0", "pr": 1, "runs": []}, jobs, skipped_jobs=0)
    extra = found["extra"]
    assert extra["in_security_scan"] == 3
    assert extra["double_trigger"] == 3
    assert extra["matrix"] == 3
    assert extra.get("cross_workflow", 0) == 0
    assert found["tests_run"] == 12
    assert found["tests_run"] - sum(extra.values()) == 3  # the first run of each test is coverage, the rest repeats
    assert {r["relation"] for r in found["redundant"]} == {"security-scan", "double-trigger"}


# ------------------------------------------------------------------------------------------------- DORA


def test_patch_release_within_72h_counts_as_a_failed_change() -> None:
    until = datetime(2026, 10, 18, tzinfo=UTC)
    since = until - timedelta(days=30)
    signals = RepoSignals(None, "o/r", since, until, {})  # type: ignore[arg-type]
    t0 = since + timedelta(days=5)
    releases = [
        {"tag": "v1.2.0", "t": t0, "pre": False},
        {"tag": "v1.2.1", "t": t0 + timedelta(hours=20), "pre": False},  # hotfix of 1.2.0
        {"tag": "v1.3.0", "t": t0 + timedelta(days=10), "pre": False},
    ]
    pr = {"merged": t0 - timedelta(hours=5), "created": t0 - timedelta(hours=29), "author": "human"}
    dora = signals.dora([], [pr], releases, revert_times=[])
    assert dora["source"] == "releases"
    assert dora["deployments"] == 3
    assert dora["cfr_failed"] == 1
    assert dora["lead_time_h"] == 29.0


# ------------------------------------------------------------------------------------------------- report


def test_report_data_cannot_close_its_script_tag() -> None:
    page = report.bundle(
        {"meta": {}, "runs": [{"workflow": "</script><script>alert(1)</script>"}]},
        title="T",
        heading="H",
        subtitle="S",
        description="D",
        document=True,
    )
    data = page.split('<script type="application/json" id="obs-data">', 1)[1].split("</script>", 1)[0]
    assert "<" not in data
    assert json.loads(data)["runs"][0]["workflow"].startswith("</script>")
    assert page.startswith("<!doctype html>")


def test_shared_command_leaves_out_local_options() -> None:
    argv = ["--org", "acme", "--out", "/Users/someone/reports", "--title", "Payments CI", "--open", "--out=/tmp/x"]
    argv += ["--fixes", "/Users/someone/fixes.json", "--fixes=/tmp/f.json"]
    assert shareable(argv) == "ci-observatory --org acme --title 'Payments CI'"


def test_measured_fixes_reach_the_report(tmp_path: Path) -> None:
    (tmp_path / "runs.json").write_text("[]")
    fixes = {"fixes": [{"repo": "acme/api", "status": "measured", "title": "Stop sleeping in teardown"}]}
    (tmp_path / "fixes.json").write_text(json.dumps(fixes))
    paths = {key: tmp_path / name for key, name in report.DATA_FILES.items()}
    out = report.build(paths, tmp_path / "index.html", heading="H")
    data = out.read_text().split('<script type="application/json" id="obs-data">', 1)[1].split("</script>", 1)[0]
    assert json.loads(data)["fixes"] == fixes


def test_demo_fixes_are_complete() -> None:
    """Every demo fix has what the Solution box needs, and its numbers come with evidence."""
    demo = json.loads((Path(__file__).parent.parent / "demo" / "data" / "fixes.json").read_text())
    for fix in demo["fixes"]:
        assert fix["phase"] in (1, 2, 3, 4) and fix["sev"] in ("crit", "high", "med", "low") and fix["effort"] in "SML"
        assert fix["status"] in ("measured", "proposed") and fix["fix"] and fix["gain"] and fix["tradeoff"]
        assert fix["evidence"] and all(e["url"].startswith("https://") for e in fix["evidence"])


class ListingGh:
    """Discovery's view of one organisation: an active repository, a fork, an archived one, and an idle one."""

    def json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        fresh, idle = "2026-10-10T00:00:00Z", "2025-01-01T00:00:00Z"

        def repo(name: str, pushed: str, fork: bool = False, archived: bool = False) -> dict[str, Any]:
            return {"full_name": name, "fork": fork, "archived": archived, "pushed_at": pushed, "visibility": "public"}

        if path == "user":
            return {"login": "me"}
        if path == "orgs/acme":
            return {"login": "acme"}
        if path == "orgs/acme/repos" and (params or {}).get("page") == 1:
            return [
                repo("acme/api", fresh),
                repo("acme/kafka", fresh, fork=True),
                repo("acme/old", fresh, archived=True),
                repo("acme/cron", idle),
            ]
        return []


def test_discovery_keeps_forks_and_idle_repositories_only_when_asked() -> None:
    since = datetime(2026, 9, 19, tzinfo=UTC)
    gh: Any = ListingGh()
    assert [r["full_name"] for r in discover(gh, ["acme"], since, "public")] == ["acme/api"]
    every = [r["full_name"] for r in discover(gh, ["acme"], since, "public", all_active=True)]
    assert every == ["acme/api", "acme/kafka", "acme/cron"]
