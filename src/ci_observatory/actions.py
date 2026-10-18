#!/usr/bin/env python3
"""Measure how CI/CD feels for engineers in GitHub repositories, using read-only GitHub API calls.

It reads GitHub Actions data (workflows, runs, jobs and steps), pull requests and releases through the GitHub CLI
(`gh api --method GET ...`), so it never sees your token, and writes a summary JSON file plus one table per entity
(runs, jobs, steps, workflows, pull requests, releases, repositories). No personal data is kept: no logins, names or
emails, only timestamps, counts and workflow names. Private repositories work like public ones when your gh login
(or GH_TOKEN) can read them.

    python -m ci_observatory.actions --orgs my-org --dry-run          # show which repositories it would scan
    python -m ci_observatory.actions --orgs my-org --top 10
    python -m ci_observatory.actions --repos my-org/api my-org/web --days 30

Exit codes: 0 done, 1 done but incomplete (rate limit or call budget reached), 2 bad usage or `gh` missing.
"""

import argparse
import json
import math
import re
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from threading import Lock
from typing import Any

from ci_observatory.gh import BudgetExceeded, Gh, GhError, RateLimited

SCHEMA_VERSION = 2
API_VERSION = "2022-11-28"
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]*/[A-Za-z0-9_.][A-Za-z0-9_.-]*$")

# Events that wait on a developer. Scheduled and manual runs are counted as volume but not as "developer waiting".
DEV_EVENTS = frozenset({"pull_request", "push", "merge_group", "pull_request_target"})
DECIDED = frozenset({"success", "failure", "timed_out", "startup_failure"})
GREEN = frozenset({"success", "skipped", "neutral"})

PHASES = ("setup", "lint", "test", "build", "security", "release", "other")

# The first matching rule wins. Step names come from `name:` or, when a step has none, from "Run <action or command>".
# This is a heuristic on names, so the output also lists the longest steps and the share left in "other".
PHASE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "setup",
        re.compile(
            r"^(set up job|complete job|initialize containers|stop containers|post )"
            r"|checkout|set ?up|setup-|install|cache|restore|download|\bsync\b|login|log in|configure|prepare|qemu|buildx"
            r"|bootstrap|dependencies|\bdeps\b|\bapt\b|pnpm|runner_|extract version|resolve .*version|^env$|cpu cores"
            r"|determine if"
        ),
    ),
    (
        "security",
        re.compile(
            r"codeql|scan|trivy|gitleaks|semgrep|dependency-review|snyk|sbom|grype|sonar|scorecard|osv-scanner|bandit"
            r"|secret|analy[sz]e|vulnerab|license"
        ),
    ),
    (
        "lint",
        re.compile(
            r"lint|ruff|flake8|pylint|mypy|pyright|black|isort|eslint|prettier|golangci|gofmt|go vet|\bvet\b|shellcheck"
            r"|pre-commit|format|typecheck|type-check|type check|commitlint|yamllint|hadolint|spell|trunk|\btsc\b|\bfmt\b"
            r"|static check|uncommitted"
        ),
    ),
    (
        "test",
        re.compile(
            r"\btests?|pytest|jest|vitest|unittest|integration|e2e|acceptance|coverage|codecov|\btox\b|\bnox\b|junit"
            r"|gotestsum|ginkgo|envtest|kuttl|benchmark"
        ),
    ),
    (
        "build",
        re.compile(
            r"build|compile|package|wheel|sdist|docker|image|gradle|maven|mvn|generate|codegen|sphinx|gen docs|kustomize"
            r"|make clean"
        ),
    ),
    (
        "release",
        re.compile(r"publish|deploy|release|upload|push|pypi|twine|goreleaser|semantic|\btag\b|sign|attest|archive"),
    ),
)

RUNNER_CLASSES = ("ubuntu", "macos", "windows", "self-hosted", "other")


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def when(value: str) -> datetime:
    return datetime.fromisoformat(value)


def seconds_between(start: str | None, end: str | None) -> float | None:
    a, b = parse_time(start), parse_time(end)
    if a is None or b is None:
        return None
    return (b - a).total_seconds()


def percentile(values: Sequence[float], p: float) -> float | None:
    """Linear interpolation between closest ranks (the same as PostgreSQL's percentile_cont)."""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * p
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def rounded(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def share(part: float, whole: float) -> float | None:
    return None if whole == 0 else round(part / whole, 4)


def classify_step(name: str) -> str:
    lowered = name.lower()
    for phase, pattern in PHASE_RULES:
        if pattern.search(lowered):
            return phase
    return "other"


def normalise_job_name(name: str) -> str:
    """'test (3.13, ubuntu-latest)' and 'test (3.12, ubuntu-latest)' are the same job."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", name).strip() or name


def runner_class(labels: Sequence[str]) -> str:
    joined = " ".join(labels).lower()
    for name in ("self-hosted", "macos", "windows", "ubuntu"):
        if name in joined:
            return name
    return "other"


def week_start(moment: datetime) -> str:
    return (moment - timedelta(days=moment.weekday())).date().isoformat()


def weeks_between(start: datetime, end: datetime) -> list[str]:
    weeks: list[str] = []
    current = datetime.fromisoformat(week_start(start)).replace(tzinfo=UTC)
    while current <= end:
        weeks.append(current.date().isoformat())
        current += timedelta(days=7)
    return weeks


# ---------------------------------------------------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------------------------------------------------


def discover(gh: Gh, orgs: Sequence[str], since: datetime, visibility: str = "all") -> list[dict[str, Any]]:
    """Active, non-fork, non-archived repositories pushed to inside the window. `orgs` may also name user accounts:
    your own account lists your private repositories too, other users' accounts list their public ones."""
    try:
        me = str((gh.json("user") or {}).get("login") or "")
    except GhError:
        me = ""  # a GitHub Actions token cannot read /user
    found: list[dict[str, Any]] = []
    for org in orgs:
        if org.lower() == str(me).lower():
            path, params = "user/repos", {"affiliation": "owner", "visibility": visibility}
        elif gh.json(f"orgs/{org}") is not None:
            path, params = f"orgs/{org}/repos", {"type": visibility}
        else:
            path, params = f"users/{org}/repos", {"type": "owner"}
        for page in range(1, 11):
            batch = gh.json(path, {**params, "sort": "pushed", "direction": "desc", "per_page": 100, "page": page})
            if not batch:
                break
            for repo in batch:
                pushed = parse_time(repo.get("pushed_at"))
                if repo["fork"] or repo["archived"] or repo.get("disabled") or pushed is None or pushed < since:
                    continue
                if visibility != "all" and repo.get("visibility", "public") != visibility:
                    continue
                found.append({"full_name": repo["full_name"], "language": repo.get("language")})
            if len(batch) < 100:
                break
    return found


def count_runs(gh: Gh, full_name: str, since: datetime) -> int:
    data = gh.json(
        f"repos/{full_name}/actions/runs",
        {"per_page": 1, "status": "completed", "created": f">={since:%Y-%m-%d}"},
    )
    return int(data["total_count"]) if data else 0


# ---------------------------------------------------------------------------------------------------------------------
# Collection for one repository
# ---------------------------------------------------------------------------------------------------------------------


def fetch_runs(gh: Gh, full_name: str, since: datetime, max_runs: int) -> tuple[list[dict[str, Any]], bool]:
    """Newest completed runs first. Returns the runs and whether the window was cut short by `max_runs`."""
    runs: list[dict[str, Any]] = []
    page = 1
    while len(runs) < max_runs:
        data = gh.json(
            f"repos/{full_name}/actions/runs",
            {"per_page": 100, "page": page, "status": "completed", "created": f">={since:%Y-%m-%d}"},
        )
        batch = data["workflow_runs"] if data else []
        runs.extend(batch)
        if len(batch) < 100:
            return runs, False
        page += 1
    return runs[:max_runs], True


def pick_runs_for_jobs(runs: Sequence[dict[str, Any]], per_workflow: int, cap: int) -> list[dict[str, Any]]:
    """Newest decided developer-facing runs, spread evenly across workflows so a noisy one cannot crowd out the rest."""
    queues: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        if run["event"] in DEV_EVENTS and run["conclusion"] in ("success", "failure"):
            queues[run["workflow_id"]].append(run)
    chosen: list[dict[str, Any]] = []
    for round_number in range(per_workflow):
        for queue in sorted(queues.values(), key=len, reverse=True):
            if round_number < len(queue) and len(chosen) < cap:
                chosen.append(queue[round_number])
    return chosen


def slim_job(job: dict[str, Any], run_started: str | None) -> dict[str, Any] | None:
    """Keep only timings and names from a job. Skipped jobs and jobs that never ran are dropped."""
    if job.get("conclusion") == "skipped" or not job.get("started_at") or not job.get("completed_at"):
        return None
    duration = seconds_between(job["started_at"], job["completed_at"])
    queue = seconds_between(job.get("created_at"), job["started_at"])
    if duration is None or duration < 0:
        return None
    steps: list[dict[str, Any]] = []
    for step in job.get("steps") or []:
        length = seconds_between(step.get("started_at"), step.get("completed_at"))
        if length is None or length < 0 or step.get("conclusion") == "skipped":
            continue
        offset = seconds_between(run_started, step["started_at"])
        steps.append(
            {"name": step["name"], "phase": classify_step(step["name"]), "s": length, "at": max(offset or 0.0, 0.0)}
        )
    offset = seconds_between(run_started, job["started_at"])
    return {
        "name": normalise_job_name(job["name"]),
        "full": job["name"],
        "ok": job.get("conclusion") == "success",
        "queue": max(queue or 0.0, 0.0),
        "dur": duration,
        "at": max(offset or 0.0, 0.0),
        "runner": runner_class(job.get("labels") or []),
        "steps": steps,
    }


def fetch_jobs(gh: Gh, full_name: str, runs: Sequence[dict[str, Any]]) -> dict[int, list[dict[str, Any]]]:
    def one(run: dict[str, Any]) -> tuple[int, list[dict[str, Any]]]:
        data = gh.json(f"repos/{full_name}/actions/runs/{run['id']}/jobs", {"per_page": 100, "filter": "latest"})
        jobs = [slim_job(j, run.get("run_started_at")) for j in (data["jobs"] if data else [])]
        return run["id"], [j for j in jobs if j]

    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(pool.map(one, runs))


def analyse_workflow_text(text: str) -> dict[str, Any]:
    """Regular-expression checks on one workflow file. Good enough for a survey, not a parser."""
    uses = re.findall(r"^\s*-?\s*uses:\s*([^\s#]+)", text, re.M)
    external = [u for u in uses if not u.startswith(("./", "docker://"))]
    return {
        "external_actions": len(external),
        "pinned_actions": sum(1 for u in external if re.search(r"@[0-9a-f]{40}$", u)),
        "permissions": bool(re.search(r"^permissions:", text, re.M)),
        "concurrency": bool(re.search(r"^concurrency:", text, re.M)),
        "timeouts": bool(re.search(r"^\s+timeout-minutes:", text, re.M)),
        "cache": bool(re.search(r"actions/cache@|^\s+cache:\s*\S|enable-cache|cache-from:", text, re.M)),
        "matrix": bool(re.search(r"^\s+matrix:", text, re.M)),
        "codeql": "github/codeql-action" in text,
        "pull_request_target": bool(re.search(r"\bpull_request_target\b", text)),
        "self_hosted": "self-hosted" in text,
    }


def fetch_hygiene(gh: Gh, full_name: str, workflows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    files = [w for w in workflows if w["state"] == "active" and w["path"].startswith(".github/workflows/")][:12]
    texts = [t for t in (gh.text(f"repos/{full_name}/contents/{w['path']}") for w in files) if t]
    parts = [analyse_workflow_text(t) for t in texts]
    dependabot = gh.text(f"repos/{full_name}/contents/.github/dependabot.yml")
    if dependabot is None:
        dependabot = gh.text(f"repos/{full_name}/contents/.github/dependabot.yaml")
    external = sum(p["external_actions"] for p in parts)
    return {
        "files": len(parts),
        "external_actions": external,
        "pinned_actions": sum(p["pinned_actions"] for p in parts),
        "pinned_share": share(sum(p["pinned_actions"] for p in parts), external),
        "permissions": sum(p["permissions"] for p in parts),
        "concurrency": sum(p["concurrency"] for p in parts),
        "timeouts": sum(p["timeouts"] for p in parts),
        "cache": sum(p["cache"] for p in parts),
        "matrix": sum(p["matrix"] for p in parts),
        "codeql": any(p["codeql"] for p in parts),
        "pull_request_target": sum(p["pull_request_target"] for p in parts),
        "self_hosted": any(p["self_hosted"] for p in parts),
        "dependabot": dependabot is not None,
        "dependabot_actions": bool(dependabot and "github-actions" in dependabot),
    }


# ---------------------------------------------------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------------------------------------------------


def pr_commit_rows(runs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """For every pull request commit that ended green: when it started, elapsed seconds and critical path seconds.

    Elapsed runs from the first workflow run on the commit to the last workflow finishing, so it includes the wait
    before a rerun. The critical path is the longest final attempt, which is what the checks themselves cost.
    """
    by_sha: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        if run["event"] == "pull_request":
            by_sha[run["head_sha"]].append(run)
    rows: list[dict[str, Any]] = []
    for sha, group in by_sha.items():
        latest: dict[int, dict[str, Any]] = {}
        for run in group:
            current = latest.get(run["workflow_id"])
            if current is None or run["run_number"] > current["run_number"]:
                latest[run["workflow_id"]] = run
        finals = list(latest.values())
        if any(r["conclusion"] not in GREEN for r in finals) or not any(r["conclusion"] == "success" for r in finals):
            continue
        first = min(when(r["created_at"]) for r in group)
        last = max(when(r["updated_at"]) for r in finals)
        path = max(seconds_between(r.get("run_started_at"), r["updated_at"]) or 0.0 for r in finals)
        rows.append(
            {
                "sha": sha[:7],
                "started": first.isoformat().replace("+00:00", "Z"),
                "elapsed_s": round((last - first).total_seconds()),
                "critical_s": round(path),
                "workflows": len(finals),
            }
        )
    return rows


def pr_commit_times(runs: Sequence[dict[str, Any]]) -> list[tuple[float, float]]:
    """(elapsed, critical path) seconds for every green pull request commit."""
    return [(row["elapsed_s"], row["critical_s"]) for row in pr_commit_rows(runs)]


def summarise_workflows(runs: Sequence[dict[str, Any]], workflows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        by_id[run["workflow_id"]].append(run)
    out: list[dict[str, Any]] = []
    for workflow in workflows:
        group = by_id.get(workflow["id"], [])
        decided = [r for r in group if r["conclusion"] in DECIDED]
        walls = [
            w
            for r in group
            if r["conclusion"] == "success" and (w := seconds_between(r.get("run_started_at"), r["updated_at"])) is not None
        ]
        out.append(
            {
                "name": workflow["name"],
                "runs": len(group),
                "success_rate": share(sum(r["conclusion"] == "success" for r in decided), len(decided)),
                "wall_p50": rounded(percentile(walls, 0.5)),
                "wall_p95": rounded(percentile(walls, 0.95)),
            }
        )
    return sorted(out, key=lambda w: -w["runs"])


def summarise_jobs(
    sampled: dict[int, list[dict[str, Any]]], runs: Sequence[dict[str, Any]], covered_days: float
) -> dict[str, Any]:
    """Job, step and phase numbers. Phase time is weighted by how often each workflow runs, so cheap and frequent
    workflows count as often as they really happen and the totals add up to a believable amount of compute."""
    by_id = {r["id"]: r for r in runs}
    dev_count: Counter[int] = Counter(r["workflow_id"] for r in runs if r["event"] in DEV_EVENTS)
    groups: dict[int, list[list[dict[str, Any]]]] = defaultdict(list)
    names: dict[int, str] = {}
    for run_id, jobs in sampled.items():
        run = by_id[run_id]
        groups[run["workflow_id"]].append(jobs)
        names[run["workflow_id"]] = run["name"]
    runs_per_week = 7 / covered_days
    workflows: list[dict[str, Any]] = []
    for workflow_id, job_lists in groups.items():
        phase_mean = {
            p: statistics.fmean(sum(s["s"] for j in jobs for s in j["steps"] if s["phase"] == p) for jobs in job_lists)
            for p in PHASES
        }
        compute = statistics.fmean(sum(j["dur"] for j in jobs) for jobs in job_lists)
        weekly_runs = dev_count[workflow_id] * runs_per_week
        workflows.append(
            {
                "name": names[workflow_id],
                "dev_runs": dev_count[workflow_id],
                "sampled": len(job_lists),
                "runs_per_week": round(weekly_runs, 1),
                "jobs_per_run": round(statistics.fmean(len(jobs) for jobs in job_lists), 1),
                "compute_s": round(compute, 1),
                "phase_s": {p: round(v, 1) for p, v in phase_mean.items()},
                "compute_min_week": round(weekly_runs * compute / 60, 1),
                "phase_min_week": {p: round(weekly_runs * v / 60, 2) for p, v in phase_mean.items()},
            }
        )
    weight = sum(w["dev_runs"] for w in workflows)
    all_jobs = [j for jobs in sampled.values() for j in jobs]
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for job in all_jobs:
        by_name[job["name"]].append(job)
    runner_seconds: Counter[str] = Counter()
    for job in all_jobs:
        runner_seconds[job["runner"]] += job["dur"]
    total_seconds = sum(runner_seconds.values())
    top_jobs: list[dict[str, Any]] = [
        {
            "name": name,
            "n": len(jobs),
            "dur_p50": rounded(percentile([j["dur"] for j in jobs], 0.5)),
            "queue_p50": rounded(percentile([j["queue"] for j in jobs], 0.5)),
            "ok": share(sum(j["ok"] for j in jobs), len(jobs)),
        }
        for name, jobs in by_name.items()
    ]
    top_jobs.sort(key=lambda j: -(j["dur_p50"] or 0))
    workflows.sort(key=lambda w: -w["compute_min_week"])
    return {
        "runs": len(sampled),
        "jobs": len(all_jobs),
        "queue_p50": rounded(percentile([j["queue"] for j in all_jobs], 0.5)),
        "queue_p95": rounded(percentile([j["queue"] for j in all_jobs], 0.95)),
        "compute_s": round(sum(w["dev_runs"] * w["compute_s"] for w in workflows) / weight, 1) if weight else 0.0,
        "phase_s": {
            p: round(sum(w["dev_runs"] * w["phase_s"][p] for w in workflows) / weight, 1) if weight else 0.0 for p in PHASES
        },
        "compute_min_week": round(sum(w["compute_min_week"] for w in workflows), 1),
        "phase_min_week": {p: round(sum(w["phase_min_week"][p] for w in workflows), 2) for p in PHASES},
        "runner_share": {c: share(runner_seconds[c], total_seconds) or 0.0 for c in RUNNER_CLASSES if runner_seconds[c]},
        "workflows": workflows[:6],
        "all_workflows": workflows,
        "top_jobs": top_jobs[:8],
    }


def summarise_pulls(pulls: Sequence[dict[str, Any]], since: datetime) -> dict[str, Any]:
    humans: list[float] = []
    bots = 0
    merged = 0
    for pull in pulls:
        merged_at = parse_time(pull.get("merged_at"))
        if merged_at is None or merged_at < since:
            continue
        merged += 1
        if (pull.get("user") or {}).get("type") == "Bot":
            bots += 1
            continue
        seconds = seconds_between(pull["created_at"], pull["merged_at"])
        if seconds is not None:
            humans.append(seconds / 3600)
    return {
        "merged": merged,
        "bot_share": share(bots, merged),
        "human_merged": len(humans),
        "lead_h_p25": rounded(percentile(humans, 0.25)),
        "lead_h_p50": rounded(percentile(humans, 0.5)),
        "lead_h_p75": rounded(percentile(humans, 0.75)),
        "lead_h_p90": rounded(percentile(humans, 0.9)),
    }


def summarise_releases(releases: Sequence[dict[str, Any]], now: datetime) -> dict[str, Any]:
    stamps = sorted(t for r in releases if not r.get("draft") and (t := parse_time(r.get("published_at"))) is not None)
    recent = [t for t in stamps if t >= now - timedelta(days=90)]
    gaps = [(b - a).total_seconds() / 86400 for a, b in pairwise(recent)]
    return {"last_90d": len(recent), "gap_days_p50": rounded(percentile(gaps, 0.5))}


def summarise_runs(
    runs: Sequence[dict[str, Any]], weeks: Sequence[str], covered_from: datetime, now: datetime, days: int
) -> dict[str, Any]:
    events = Counter(r["event"] for r in runs)
    conclusions = Counter(r["conclusion"] for r in runs)
    dev = [r for r in runs if r["event"] in DEV_EVENTS]
    decided = [r for r in dev if r["conclusion"] in DECIDED]
    successes = [r for r in dev if r["conclusion"] == "success"]
    times = pr_commit_times(runs)
    elapsed = [t[0] for t in times]
    critical = [t[1] for t in times]
    weekly: Counter[str] = Counter(week_start(when(r["created_at"])) for r in runs)
    first_full_week = week_start(covered_from + timedelta(days=7))
    covered_days = max((now - covered_from).total_seconds() / 86400, 1.0)
    return {
        "runs": len(runs),
        "runs_per_week": rounded(len(runs) / (covered_days / 7)),
        "events": dict(events.most_common()),
        "conclusions": dict(conclusions.most_common()),
        "dev_runs": len(dev),
        "dev_conclusions": dict(Counter(r["conclusion"] for r in dev).most_common()),
        "success_rate": share(sum(r["conclusion"] == "success" for r in decided), len(decided)),
        "cancelled_share": share(sum(r["conclusion"] == "cancelled" for r in dev), len(dev)),
        "rerun_share": share(sum(r["run_attempt"] > 1 for r in dev), len(dev)),
        "recovered_by_rerun": share(sum(r["run_attempt"] > 1 for r in successes), len(successes)),
        "dependabot_share": share(sum((r.get("head_branch") or "").startswith("dependabot/") for r in dev), len(dev)),
        "ttg_n": len(times),
        "ttg_p50": rounded(percentile(elapsed, 0.5)),
        "ttg_p90": rounded(percentile(elapsed, 0.9)),
        "cp_p50": rounded(percentile(critical, 0.5)),
        "cp_p90": rounded(percentile(critical, 0.9)),
        "weekly": [weekly.get(w, 0) if w >= first_full_week else None for w in weeks],
        "window_days": days,
        "covered_days": round(covered_days, 1),
    }


def build_anatomy(
    repo: str, runs: Sequence[dict[str, Any]], sampled: dict[int, list[dict[str, Any]]]
) -> dict[str, Any] | None:
    """One run drawn as a timeline: a pull request run of the workflow that costs the most compute, 3 to 12 jobs,
    with the wall time closest to that workflow's median."""
    dev_count: Counter[int] = Counter(r["workflow_id"] for r in runs if r["event"] in DEV_EVENTS)
    fits = [r for r in runs if r["conclusion"] == "success" and 3 <= len(sampled.get(r["id"], [])) <= 12]
    candidates = [r for r in fits if r["event"] == "pull_request"] or fits
    by_workflow: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for run in candidates:
        by_workflow[run["workflow_id"]].append(run)
    if not by_workflow:
        return None

    def weight(workflow_id: int) -> float:
        group = by_workflow[workflow_id]
        return dev_count[workflow_id] * statistics.fmean(sum(j["dur"] for j in sampled[r["id"]]) for r in group)

    best = max(by_workflow, key=weight)
    group = by_workflow[best]
    walls = {r["id"]: seconds_between(r.get("run_started_at"), r["updated_at"]) or 0.0 for r in group}
    target = statistics.median(walls.values())
    chosen = min(group, key=lambda r: abs(walls[r["id"]] - target))
    return {
        "repo": repo,
        "workflow": chosen["name"],
        "event": chosen["event"],
        "wall_s": rounded(walls[chosen["id"]]),
        "weight": round(weight(best)),
        "jobs": sorted(sampled[chosen["id"]], key=lambda j: j["at"]),
    }


def pooled(
    runs: Sequence[dict[str, Any]],
    sampled: dict[int, list[dict[str, Any]]],
    pulls: Sequence[dict[str, Any]],
    since: datetime,
) -> dict[str, Any]:
    """Raw values for the all-repository numbers. A median of medians would be wrong, so keep the values."""
    dev = [r for r in runs if r["event"] in DEV_EVENTS]
    successes = [r for r in dev if r["conclusion"] == "success"]
    jobs = [j for group in sampled.values() for j in group]
    merged = [p for p in pulls if (t := parse_time(p.get("merged_at"))) is not None and t >= since]
    lead = [
        seconds / 3600
        for p in merged
        if (p.get("user") or {}).get("type") != "Bot"
        and (seconds := seconds_between(p["created_at"], p["merged_at"])) is not None
    ]
    times = pr_commit_times(runs)
    return {
        "elapsed": [t[0] for t in times],
        "critical": [t[1] for t in times],
        "queue": [j["queue"] for j in jobs],
        "decided": sum(r["conclusion"] in DECIDED for r in dev),
        "success": len(successes),
        "recovered": sum(r["run_attempt"] > 1 for r in successes),
        "lead_h": lead,
        "merged": len(merged),
        "steps": sum(len(j["steps"]) for j in jobs),
    }


def iso(value: str | None) -> str | None:
    return value.replace("+00:00", "Z") if value else None


def dashboard_tables(
    repo: str,
    runs: Sequence[dict[str, Any]],
    sampled: dict[int, list[dict[str, Any]]],
    workflows: Sequence[dict[str, Any]],
    pulls: Sequence[dict[str, Any]],
    releases: Sequence[dict[str, Any]],
    all_workflows: Sequence[dict[str, Any]],
    since: datetime,
    now: datetime,
) -> dict[str, list[dict[str, Any]]]:
    """Row-level tables for one repository. Only timestamps, counts and workflow, job and step names; no people."""
    run_by_id = {r["id"]: r for r in runs}
    run_rows = [
        {
            "id": r["id"],
            "repo": repo,
            "workflow": r["name"],
            "event": r["event"],
            "result": r["conclusion"],
            "ts": iso(r["created_at"]),
            "wall_s": None if (w := seconds_between(r.get("run_started_at"), r["updated_at"])) is None else round(w),
            "attempt": r["run_attempt"],
            "sha": r["head_sha"][:7] if r["event"] == "pull_request" else "",
            "bot": int((r.get("head_branch") or "").startswith("dependabot/")),
        }
        for r in runs
    ]
    job_rows: list[dict[str, Any]] = []
    step_rows: list[dict[str, Any]] = []
    for run_id, jobs in sampled.items():
        run = run_by_id[run_id]
        for job in jobs:
            by_phase: dict[str, float] = dict.fromkeys(PHASES, 0.0)
            for step in job["steps"]:
                by_phase[step["phase"]] += step["s"]
                step_rows.append(
                    {
                        "run_id": run_id,
                        "job": job["full"],
                        "phase": step["phase"],
                        "name": step["name"],
                        "at_s": round(step["at"]),
                        "s": round(step["s"]),
                    }
                )
            job_rows.append(
                {
                    "run_id": run_id,
                    "repo": repo,
                    "workflow": run["name"],
                    "event": run["event"],
                    "started": iso(run.get("run_started_at") or run["created_at"]),
                    "job": job["full"],
                    "job_group": job["name"],
                    "at_s": round(job["at"]),
                    "queue_s": round(job["queue"]),
                    "dur_s": round(job["dur"]),
                    "ok": int(job["ok"]),
                    "runner": job["runner"],
                    **{f"{p}_s": round(v) for p, v in by_phase.items()},
                }
            )
    sampled_names = {w["name"]: w for w in all_workflows}
    workflow_rows = []
    for workflow in workflows:
        stats = sampled_names.get(workflow["name"])
        workflow_rows.append(
            {
                "repo": repo,
                "workflow": workflow["name"],
                "state": workflow["state"],
                "sampled_runs": stats["sampled"] if stats else 0,
                "jobs_per_run": stats["jobs_per_run"] if stats else None,
                "compute_s": stats["compute_s"] if stats else None,
                **{f"{p}_s": (stats["phase_s"][p] if stats else None) for p in PHASES},
            }
        )
    merged_rows = []
    for pull in pulls:
        merged_at = parse_time(pull.get("merged_at"))
        if merged_at is None or merged_at < since:
            continue
        seconds = seconds_between(pull["created_at"], pull["merged_at"]) or 0.0
        merged_rows.append(
            {
                "repo": repo,
                "merged": iso(pull["merged_at"]),
                "lead_h": round(seconds / 3600, 2),
                "bot": int((pull.get("user") or {}).get("type") == "Bot"),
            }
        )
    release_rows = [
        {"repo": repo, "published": iso(r["published_at"])}
        for r in releases
        if not r.get("draft") and (t := parse_time(r.get("published_at"))) is not None and t >= now - timedelta(days=90)
    ]
    return {
        "runs": run_rows,
        "pr_commits": [{"repo": repo, **row} for row in pr_commit_rows(runs)],
        "jobs": job_rows,
        "steps": step_rows,
        "workflows": workflow_rows,
        "pulls": merged_rows,
        "releases": release_rows,
    }


def repo_row(summary: dict[str, Any]) -> dict[str, Any]:
    """One flat row per repository for the dashboard's repository table."""
    runs, jobs, pulls, hygiene = summary["runs"], summary["jobs"], summary["pulls"], summary["hygiene"]
    return {
        "repo": summary["repo"],
        "org": summary["org"],
        "name": summary["repo"].split("/")[1],
        "language": summary["language"],
        "truncated": int(summary["truncated"]),
        "covered_days": runs["covered_days"],
        "runs": runs["runs"],
        "runs_per_week": runs["runs_per_week"],
        "workflow_files": summary["workflow_files"],
        "compute_min_week": jobs["compute_min_week"],
        "compute_s_per_run": jobs["compute_s"],
        **{f"phase_{p}_s": jobs["phase_s"][p] for p in PHASES},
        "pr_bot_share": pulls["bot_share"],
        "releases_90d": summary["releases"]["last_90d"],
        "hyg_files": hygiene["files"],
        "hyg_external_actions": hygiene["external_actions"],
        "hyg_pinned_actions": hygiene["pinned_actions"],
        "hyg_permissions": hygiene["permissions"],
        "hyg_concurrency": hygiene["concurrency"],
        "hyg_timeouts": hygiene["timeouts"],
        "hyg_cache": hygiene["cache"],
        "hyg_codeql": int(hygiene["codeql"]),
        "hyg_pull_request_target": hygiene["pull_request_target"],
        "hyg_dependabot": int(hygiene["dependabot"]),
        "hyg_dependabot_actions": int(hygiene["dependabot_actions"]),
    }


class Collector:
    def __init__(self, gh: Gh, args: argparse.Namespace, now: datetime) -> None:
        self.gh = gh
        self.args = args
        self.now = now
        self.since = now - timedelta(days=args.days)
        self.weeks = weeks_between(self.since, now)
        self.step_seconds: Counter[tuple[str, str]] = Counter()
        self.step_count: Counter[tuple[str, str]] = Counter()
        self._lock = Lock()

    def collect(
        self, full_name: str, language: str | None
    ) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any], dict[str, list[dict[str, Any]]]]:
        gh = self.gh
        workflows_data = gh.json(f"repos/{full_name}/actions/workflows", {"per_page": 100})
        workflows = workflows_data["workflows"] if workflows_data else []
        runs, truncated = fetch_runs(gh, full_name, self.since, self.args.max_runs)
        covered_from = min((when(r["created_at"]) for r in runs), default=self.since) if truncated else self.since
        chosen = pick_runs_for_jobs(runs, self.args.jobs_per_workflow, self.args.max_job_calls)
        sampled = fetch_jobs(gh, full_name, chosen)
        pulls = gh.json(
            f"repos/{full_name}/pulls",
            {"state": "closed", "sort": "updated", "direction": "desc", "per_page": 100},
        )
        releases = gh.json(f"repos/{full_name}/releases", {"per_page": 100})
        hygiene = fetch_hygiene(gh, full_name, workflows)
        with self._lock:
            for jobs in sampled.values():
                for job in jobs:
                    for step in job["steps"]:
                        key = (step["name"], step["phase"])
                        self.step_seconds[key] += step["s"]
                        self.step_count[key] += 1
        run_summary = summarise_runs(runs, self.weeks, covered_from, self.now, self.args.days)
        job_summary = summarise_jobs(sampled, runs, run_summary["covered_days"])
        all_workflows = job_summary.pop("all_workflows")
        tables = dashboard_tables(
            full_name, runs, sampled, workflows, pulls or [], releases or [], all_workflows, self.since, self.now
        )
        summary = {
            "repo": full_name,
            "org": full_name.split("/")[0],
            "language": language,
            "truncated": truncated,
            "workflows": summarise_workflows(runs, workflows)[:8],
            "workflow_files": len(workflows),
            "runs": run_summary,
            "jobs": job_summary,
            "pulls": summarise_pulls(pulls or [], self.since),
            "releases": summarise_releases(releases or [], self.now),
            "hygiene": hygiene,
        }
        return summary, build_anatomy(full_name, runs, sampled), pooled(runs, sampled, pulls or [], self.since), tables


def pick_repos(gh: Gh, args: argparse.Namespace, since: datetime) -> list[tuple[str, str | None, int | None]]:
    """(full name, language, completed runs in the window) for every repository we will scan."""
    if args.repos:
        languages: dict[str, str | None] = {}
        for name in args.repos:
            info = gh.json(f"repos/{name}")
            languages[name] = (info or {}).get("language")
        return [(name, languages[name], None) for name in args.repos]
    candidates = discover(gh, args.orgs, since, args.visibility)
    with ThreadPoolExecutor(max_workers=4) as pool:
        counts = list(pool.map(lambda c: count_runs(gh, c["full_name"], since), candidates))
    ranked: list[tuple[str, str | None, int | None]] = [
        (c["full_name"], c["language"], n) for c, n in zip(candidates, counts, strict=True) if n >= args.min_runs
    ]
    ranked.sort(key=lambda item: -(item[2] or 0))
    keep = {name.lower() for name in args.include}
    extra = [r for r in ranked[args.top :] if r[0].lower() in keep]
    return ranked[: args.top] + extra


def typical(values: Sequence[float | None]) -> dict[str, Any]:
    """The middle repository and the spread: every repository counts once, whatever its volume."""
    kept = [v for v in values if v is not None]
    return {
        "median": rounded(percentile(kept, 0.5), 3),
        "min": rounded(min(kept), 3) if kept else None,
        "max": rounded(max(kept), 3) if kept else None,
        "n": len(kept),
    }


def build_totals(
    repos: Sequence[dict[str, Any]], pools: Sequence[dict[str, Any]], steps: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """Headline numbers over every repository, pooled from raw values rather than averaged from per-repo medians."""

    def flat(key: str) -> list[float]:
        return [x for pool in pools for x in pool[key]]

    elapsed, critical, queue, lead = flat("elapsed"), flat("critical"), flat("queue"), flat("lead_h")
    phase_week: Counter[str] = Counter()
    runner: Counter[str] = Counter()
    for repo in repos:
        jobs = repo["jobs"]
        for phase, minutes in jobs["phase_min_week"].items():
            phase_week[phase] += minutes
        for kind, fraction in jobs["runner_share"].items():
            runner[kind] += fraction * jobs["compute_min_week"]
    external = sum(r["hygiene"]["external_actions"] for r in repos)
    merged = sum(pool["merged"] for pool in pools)
    listed = [dict(w, repo=r["repo"]) for r in repos for w in r["jobs"]["workflows"]]
    listed.sort(key=lambda w: -w["compute_min_week"])
    steps_total = sum(s["seconds"] for s in steps)
    other_steps = sum(s["seconds"] for s in steps if s["phase"] == "other")
    return {
        "repos": len(repos),
        "runs": sum(r["runs"]["runs"] for r in repos),
        "dev_runs": sum(r["runs"]["dev_runs"] for r in repos),
        "runs_sampled": sum(r["jobs"]["runs"] for r in repos),
        "jobs_sampled": sum(r["jobs"]["jobs"] for r in repos),
        "steps": sum(pool["steps"] for pool in pools),
        "workflow_files": sum(r["workflow_files"] for r in repos),
        "pr_commits": len(elapsed),
        "ttg_p50": rounded(percentile(elapsed, 0.5)),
        "ttg_p90": rounded(percentile(elapsed, 0.9)),
        "cp_p50": rounded(percentile(critical, 0.5)),
        "cp_p90": rounded(percentile(critical, 0.9)),
        "queue_p50": rounded(percentile(queue, 0.5)),
        "queue_p95": rounded(percentile(queue, 0.95)),
        "success_rate": share(sum(p["success"] for p in pools), sum(p["decided"] for p in pools)),
        "recovered_by_rerun": share(sum(p["recovered"] for p in pools), sum(p["success"] for p in pools)),
        "pr_merged": merged,
        "pr_lead_h_p50": rounded(percentile(lead, 0.5)),
        "pr_lead_h_p90": rounded(percentile(lead, 0.9)),
        "pr_bot_share": share(merged - len(lead), merged),
        "pinned_share": share(sum(r["hygiene"]["pinned_actions"] for r in repos), external),
        "compute_min_week": round(sum(phase_week.values())),
        "phase_min_week": {k: round(v) for k, v in phase_week.items()},
        "other_step_share": share(other_steps, steps_total),
        "runner_share": {k: share(v, sum(runner.values())) for k, v in runner.most_common()},
        "typical": {
            "ttg": typical([r["runs"]["ttg_p50"] for r in repos if r["runs"]["ttg_n"] >= 5]),
            "cp": typical([r["runs"]["cp_p50"] for r in repos if r["runs"]["ttg_n"] >= 5]),
            "queue_p50": typical([r["jobs"]["queue_p50"] for r in repos if r["jobs"]["jobs"] >= 10]),
            "queue_p95": typical([r["jobs"]["queue_p95"] for r in repos if r["jobs"]["jobs"] >= 10]),
            "pr_lead_h": typical([r["pulls"]["lead_h_p50"] for r in repos if r["pulls"]["human_merged"] >= 5]),
            "success": typical([r["runs"]["success_rate"] for r in repos if r["runs"]["dev_runs"] >= 20]),
            "recovered": typical([r["runs"]["recovered_by_rerun"] for r in repos if r["runs"]["dev_runs"] >= 20]),
        },
        "heavy_workflows": listed[:12],
    }


def build_insights(repos: Sequence[dict[str, Any]], totals: dict[str, Any]) -> list[dict[str, str]]:
    """Plain-language findings computed from the numbers, so they cannot drift from the charts."""
    insights: list[dict[str, str]] = []

    def minutes(seconds: float) -> str:
        if seconds < 90:
            return f"{seconds:.0f} s"
        return f"{seconds / 60:.0f} min" if seconds >= 600 else f"{seconds / 60:.1f} min"

    ttg = [r for r in repos if r["runs"]["ttg_p50"] and r["runs"]["ttg_n"] >= 5]
    if ttg:
        slow = max(ttg, key=lambda r: r["runs"]["ttg_p50"])
        fast = min(ttg, key=lambda r: r["runs"]["ttg_p50"])
        insights.append(
            {
                "title": "Slowest and fastest time to green",
                "text": f"Pull requests in {slow['repo']} wait a median {minutes(slow['runs']['ttg_p50'])} for every "
                f"check to pass, against {minutes(fast['runs']['ttg_p50'])} in {fast['repo']}.",
            }
        )
    queue = [r for r in repos if r["jobs"]["queue_p95"] is not None and r["jobs"]["jobs"] >= 10]
    if queue:
        worst = max(queue, key=lambda r: r["jobs"]["queue_p95"])
        insights.append(
            {
                "title": "Waiting for a runner",
                "text": f"A job normally starts {totals['queue_p50']:.0f} s after it is queued. The longest tail is in "
                f"{worst['repo']}, where 1 job in 20 waits {worst['jobs']['queue_p95']:.0f} s or more.",
            }
        )
    recovered = [r for r in repos if r["runs"]["recovered_by_rerun"] is not None and r["runs"]["dev_runs"] >= 30]
    if recovered:
        top = max(recovered, key=lambda r: r["runs"]["recovered_by_rerun"])
        insights.append(
            {
                "title": "Green runs that needed a rerun",
                "text": f"In {top['repo']}, {top['runs']['recovered_by_rerun'] * 100:.1f}% of green runs passed only on "
                "a later attempt. A rerun that turns green is the classic sign of a flaky test or an unreliable step.",
            }
        )
    pinned = [r for r in repos if r["hygiene"]["external_actions"] >= 5 and r["hygiene"]["pinned_share"] is not None]
    if pinned:
        pinned_all = [r for r in pinned if r["hygiene"]["pinned_share"] >= 0.95]
        none_pinned = [r for r in pinned if r["hygiene"]["pinned_share"] <= 0.05]
        insights.append(
            {
                "title": "Pinning actions to a commit",
                "text": f"Repositories that pin at least 95% of their external actions to a commit SHA: {len(pinned_all)} "
                f"of {len(pinned)}. Repositories that pin almost none: {len(none_pinned)}.",
            }
        )
    concurrency = [r for r in repos if r["hygiene"]["files"] >= 2]
    if concurrency:
        without = [r for r in concurrency if r["hygiene"]["concurrency"] == 0]
        insights.append(
            {
                "title": "Cancelling outdated runs",
                "text": f"{len(without)} of {len(concurrency)} repositories have no workflow with a top-level "
                "concurrency group, so a new push does not cancel the run it replaces.",
            }
        )
    risky = [r for r in repos if r["hygiene"]["pull_request_target"]]
    if risky:
        files = sum(r["hygiene"]["pull_request_target"] for r in risky)
        insights.append(
            {
                "title": "Workflows on pull_request_target",
                "text": f"{files} workflow files in {len(risky)} repositories use this trigger, which runs with the "
                "base repository's secrets. Each one needs a careful look at what code it checks out.",
            }
        )
    heavy = totals["heavy_workflows"]
    if heavy:
        top_workflow = heavy[0]
        whole = totals["compute_min_week"] or 1
        insights.append(
            {
                "title": "The heaviest workflow",
                "text": f"{top_workflow['name']} in {top_workflow['repo']} accounts for an estimated "
                f"{top_workflow['compute_min_week'] / 60:.0f} runner-hours a week, "
                f"{top_workflow['compute_min_week'] / whole * 100:.0f}% of the compute across the sampled repositories.",
            }
        )
    return insights


def write_tables(
    folder: Path,
    report: dict[str, Any],
    results: Sequence[tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any], dict[str, list[dict[str, Any]]]]],
) -> None:
    """One JSON file of row objects per table, the shape a dashboard dataset expects."""
    folder.mkdir(parents=True, exist_ok=True)
    tables: dict[str, list[dict[str, Any]]] = {
        name: [row for result in results for row in result[3][name]]
        for name in ("runs", "pr_commits", "jobs", "steps", "workflows", "pulls", "releases")
    }
    totals = report["totals"]
    tables["repos"] = [repo_row(result[0]) for result in results]
    tables["about"] = [
        {
            "generated_at": report["generated_at"],
            "since": report["window"]["since"],
            "until": report["window"]["until"],
            "window_days": report["window"]["days"],
            "organisations": ", ".join(report["orgs"]),
            "repos": totals["repos"],
            "runs": totals["runs"],
            "jobs_sampled": totals["jobs_sampled"],
            "steps": totals["steps"],
            "api_calls": report["api_calls"],
            "max_runs": report["max_runs"],
            "source": "GitHub REST API through the gh CLI, GET requests only",
        }
    ]
    for name, rows in tables.items():
        path = folder / f"{name}.json"
        path.write_text(json.dumps(rows, separators=(",", ":"), ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"  {path} ({len(rows)} rows, {path.stat().st_size // 1024} KB)", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ci-observatory-actions", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", type=Path, default=Path("ci-observatory/data/metrics.json"), help="summary file to write")
    parser.add_argument("--orgs", nargs="+", default=[], help="organisations or user accounts to discover repositories in")
    parser.add_argument(
        "--visibility", choices=("all", "public", "private"), default="all", help="which repositories discovery keeps"
    )
    parser.add_argument("--repos", nargs="+", metavar="OWNER/NAME", help="scan exactly these, skipping discovery")
    parser.add_argument("--include", nargs="*", default=[], metavar="OWNER/NAME", help="also scan these if active")
    parser.add_argument("--top", type=int, default=16, help="how many of the busiest repositories to scan")
    parser.add_argument("--min-runs", type=int, default=20, help="ignore repositories with fewer runs in the window")
    parser.add_argument("--days", type=int, default=45, help="length of the window")
    parser.add_argument("--max-runs", type=int, default=1000, help="most runs to read per repository (newest first)")
    parser.add_argument("--jobs-per-workflow", type=int, default=6, help="runs per workflow whose jobs are read")
    parser.add_argument("--max-job-calls", type=int, default=36, help="most job lookups per repository")
    parser.add_argument("--max-calls", type=int, default=2500, help="stop after this many API calls in total")
    parser.add_argument("--workers", type=int, default=4, help="parallel gh processes")
    parser.add_argument("--cache", default="6h", help="how long gh may reuse a response; empty string disables")
    parser.add_argument("--dash-dir", type=Path, default=Path("ci-observatory/data"), help="folder for the row-level tables")
    parser.add_argument("--dry-run", action="store_true", help="only pick the repositories and print the plan")
    args = parser.parse_args(argv)

    if not args.orgs and not args.repos:
        print("error: pass --orgs ORG [ORG ...] or --repos OWNER/NAME [...]", file=sys.stderr)
        return 2
    bad = [r for r in [*(args.repos or []), *args.include] if not REPO_PATTERN.match(r)]
    if bad:
        print(f"error: not an OWNER/NAME repository: {', '.join(bad)}", file=sys.stderr)
        return 2
    try:
        subprocess.run(["gh", "--version"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        print("error: the GitHub CLI (gh) is required: https://cli.github.com", file=sys.stderr)
        return 2

    now = datetime.now(UTC)
    since = now - timedelta(days=args.days)
    gh = Gh(args.cache, args.max_calls, args.workers)
    started = time.monotonic()
    incomplete = False
    try:
        picked = pick_repos(gh, args, since)
    except GhError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"{len(picked)} repositories, window {since:%Y-%m-%d} to {now:%Y-%m-%d}, {gh.calls} calls so far", file=sys.stderr)
    for name, _language, runs in picked:
        print(f"  {name:55} {'' if runs is None else runs:>6} runs", file=sys.stderr)
    if args.dry_run:
        return 0

    collector = Collector(gh, args, now)
    results: list[tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any], dict[str, list[dict[str, Any]]]]] = []
    lock = Lock()

    def work(item: tuple[str, str | None, int | None]) -> None:
        nonlocal incomplete
        name, language, _ = item
        try:
            outcome = collector.collect(name, language)
        except (RateLimited, BudgetExceeded, GhError) as exc:
            with lock:
                incomplete = True
            print(f"  skipped {name}: {str(exc).splitlines()[0][:120]}", file=sys.stderr)
            return
        with lock:
            results.append(outcome)
            print(f"  [{len(results)}/{len(picked)}] {name}: {outcome[0]['runs']['runs']} runs", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(work, picked))

    results.sort(key=lambda r: -r[0]["runs"]["runs"])
    repos = [r[0] for r in results]
    anatomy_options = [r[1] for r in results if r[1]]
    anatomy = max(anatomy_options, key=lambda a: (a["event"] == "pull_request", a["weight"]), default=None)
    steps = [
        {"name": name, "phase": phase, "seconds": round(seconds), "n": collector.step_count[(name, phase)]}
        for (name, phase), seconds in collector.step_seconds.most_common()
    ]
    totals = build_totals(repos, [r[2] for r in results], steps)
    report = {
        "schema": SCHEMA_VERSION,
        "generated_at": now.isoformat(timespec="seconds"),
        "window": {
            "days": args.days,
            "since": since.date().isoformat(),
            "until": now.date().isoformat(),
            "weeks": collector.weeks,
        },
        "orgs": args.orgs or sorted({r["repo"].split("/")[0] for r in repos}),
        "max_runs": args.max_runs,
        "incomplete": incomplete,
        "api_calls": gh.calls,
        "phases": list(PHASES),
        "totals": totals,
        "repos": repos,
        "anatomy": anatomy,
        "longest_steps": steps[:25],
        "unclassified_steps": [s for s in steps if s["phase"] == "other"][:15],
        "insights": build_insights(repos, totals),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    write_tables(args.dash_dir, report, results)
    print(
        f"wrote {args.out} ({len(repos)} repositories, {gh.calls} API calls, {time.monotonic() - started:.0f} s)",
        file=sys.stderr,
    )
    return 1 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
