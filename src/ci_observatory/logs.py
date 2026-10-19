#!/usr/bin/env python3
"""Read a sample of GitHub Actions job logs and turn them into telemetry, then audit tests for redundancy.

The run table written by the CI collector (runs.json) says which runs exist. This picks the latest failed and successful
runs of each repository, lists their jobs and downloads the chosen jobs' logs through the GitHub CLI
(`gh api --method GET ...`), so it never sees your token. The parser turns each log into numbers: where the job's
time went, silent stretches, cache hits, test counts, warnings, the runner image, the token's permissions and, for a
failed job, why it failed. It keeps a short, cleaned excerpt of notable lines for the report's log feed.

The test audit then takes one recent pull request commit per repository, reads every job that ran on it and matches
the tests each job ran, to find tests that run more than once and why.

Raw logs stay in a local cache (~/.cache/ci-observatory by default) and never go into the report. Excerpts drop lines
that can name a person (authors, committers, git identities), commit subjects, emails, IP addresses and anything
shaped like a token, and shorten runner paths. Private repositories need a login that can read Actions logs.

    python -m ci_observatory.logs --runs ci-observatory/data/runs.json --dry-run
    python -m ci_observatory.logs --runs ci-observatory/data/runs.json
    python -m ci_observatory.logs --offline               # re-parse cached logs without calling GitHub

Exit codes: 0 done, 1 done but some logs were missing or a limit was reached, 2 bad usage or `gh` missing.
"""

import argparse
import gzip
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from threading import Lock
from typing import Any

from ci_observatory.gh import Gh, GhError, unavailable

SCHEMA_VERSION = 1
API_VERSION = "2022-11-28"
DEV_EVENTS = frozenset({"pull_request", "push", "merge_group", "pull_request_target"})

ACTIVITIES = ("setup", "deps", "containers", "build", "test", "lint", "security", "other")
BAND_CODE = {
    "setup": "s",
    "deps": "d",
    "containers": "k",
    "build": "b",
    "test": "t",
    "lint": "l",
    "security": "x",
    "other": "o",
}

GAP_S = 20.0  # this long without a log line counts as a silent stretch
TRACE_BUCKETS = 240
FEED_LIMIT = 170
TEXT_LIMIT = 150

# What a step mostly does, from its name plus the first "Run ..." header in its log. The first matching rule wins.
STEP_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("setup", re.compile(r"^(?:set up job|complete job|post |initialize containers|stop containers)")),
    (
        "security",
        re.compile(
            r"codeql|trivy|gitleaks|semgrep|dependency-review|snyk|sbom|grype|sonar|scorecard|osv-scanner|bandit|zizmor"
            r"|gosec|govulncheck|pip-audit|vulnerab"
        ),
    ),
    (
        "lint",
        re.compile(
            r"lint|ruff|flake8|pylint|mypy|pyright|black|isort|eslint|prettier|golangci|gofmt|go vet|\bvet\b|shellcheck"
            r"|pre-commit|format|typecheck|type-check|commitlint|yamllint|hadolint|spell|checkstyle|spotless|actionlint"
            r"|\bvale\b|uncommitted|git diff --exit-code|\bstyle\b|\btrunk\b"
        ),
    ),
    (
        "test",
        re.compile(
            r"\btests?\b|pytest|jest|vitest|unittest|integration|e2e|acceptance|coverage|\btox\b|\bnox\b|junit|gotestsum"
            r"|ginkgo|envtest|kuttl|go test|\bverify\b|benchmark"
        ),
    ),
    ("containers", re.compile(r"docker|buildx|compose|podman|container|\bkind\b|minikube|qemu|ko build|\bhelm\b")),
    (
        "deps",
        re.compile(
            r"pip install|pip-sync|uv sync|uv pip|poetry install|npm ci|npm install|yarn install|pnpm install"
            r"|go mod download|go mod tidy|apt-get|apt install|brew install|bundle install|dependencies|requirements"
            r"|\bdeps\b"
        ),
    ),
    ("other", re.compile(r"publish|deploy|release|upload|pypi|twine|gh-pages|attest|\bsign\b|notify|comment")),
    (
        "build",
        re.compile(
            r"build|compile|package|wheel|sdist|gradle|maven|mvn|generate|codegen|sphinx|docusaurus|hugo|\bmake\b"
            r"|goreleaser|cargo|\bdocs?\b"
        ),
    ),
    (
        "setup",
        re.compile(r"checkout|setup-|set ?up|cache|restore|login|configure|install|download|prepare|runner|toolchain"),
    ),
)

# Lines that say what is happening right now, whatever the step is called. They override the step's activity for
# the second they are printed in. Only patterns that are hard to mistake are here.
LINE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "containers",
        re.compile(
            r"Pulling from |Pull complete|Pulling fs layer|Verifying Checksum|Digest: sha256:|Status: Downloaded"
            r"|Status: Image is up to date|^#\d+ \[|^#\d+ (?:DONE|CACHED)|^Step \d+/\d+ :|Successfully tagged"
            r"|Container \S+\s+(?:Creating|Created|Starting|Started|Healthy|Waiting|Running)|Network \S+\s+Creat"
            r"|Pulling docker image|Pulling image|Creating container for image|Starting to pull image"
        ),
    ),
    (
        "deps",
        re.compile(
            r"^\s*(?:Collecting |Downloading |Obtaining |Using cached |Installing collected packages|Successfully installed"
            r"|Requirement already satisfied|Building wheels? for)|Resolved \d+ packages|Prepared \d+ packages"
            r"|Installed \d+ packages|Audited \d+ packages|^go: downloading|added \d+ packages|^Get:\d+ |^Hit:\d+ "
            r"|^Fetched .* in |Reading package lists|^Unpacking |^Setting up |^Selecting previously unselected"
            r"|^Preparing to unpack|Downloading from |Downloaded from |^Progress \(\d+\): |Resolving dependencies"
            r"|Installing dependencies from lock file|Package operations: \d+ install|^\s+- Installing |npm (?:WARN|notice) "
        ),
    ),
    (
        "test",
        re.compile(
            r"collected \d+ items?|test session starts|::\S+ (?:PASSED|FAILED|SKIPPED|ERROR|XFAIL|XPASS)|^=== RUN"
            r"|^--- (?:PASS|FAIL|SKIP)|^(?:ok|FAIL)\s+\S+\s+[\d.]+s|^PASS$|Tests run: \d+|Running [\w.]+(?:Test|IT|Tests)$"
            r"|<<< (?:FAILURE|ERROR)!|\b\d+ passed\b|^Tests:\s+\d+"
        ),
    ),
    (
        "lint",
        re.compile(
            r"\.{4,}(?:Passed|Failed|Skipped|\(no files to check\))|^Success: no issues found|Found \d+ errors? in \d+ file"
            r"|would reformat|All checks passed!|\d+ files? (?:left unchanged|reformatted|already formatted)"
        ),
    ),
    (
        "build",
        re.compile(
            r"^\[INFO\] Building |^\[INFO\] --- (?:maven-)?(?:compiler|jar|shade|assembly)|Compiling \d+ source files?"
            r"|^> Task :|BUILD (?:SUCCESSFUL|SUCCESS|FAILURE)|^\s*Compiling \S|Successfully built \S+\.(?:whl|tar\.gz)"
            r"|\[success\] Generated static files|Finished `?(?:release|dev)"
        ),
    ),
    ("security", re.compile(r"Running queries for|codeql/|CodeQL bundle|Scanning for vulnerabilities")),
)

# Why a failed job failed. failure_reason() checks these in a fixed order against the failing step's own output.
TIMEOUT_FAILURE = re.compile(
    r"exceeded the maximum execution time|has timed out|timed out after \d+ ?min|The operation was canceled", re.I
)
INFRA_FAILURE = re.compile(
    r"No space left on device|received a shutdown signal|lost communication with the server|hosted runner"
    r" encountered an error|ECONNRESET|Connection reset by peer|Could not resolve host|Temporary failure in name"
    r" resolution|TLS handshake timeout|toomanyrequests|rate limit exceeded|503 Service Unavailable|502 Bad Gateway"
    r"|i/o timeout|Read timed out|Connection timed out|Failed to download action|Internal Server Error|EPROTO"
    r"|SSL routines|handshake failure",
    re.I,
)
DEPS_FAILURE = re.compile(
    r"No matching distribution found|Could not find a version that satisfies|ResolutionImpossible|ERESOLVE"
    r"|Could not resolve dependencies|unknown revision|Unable to locate package|version solving failed"
    r"|Could not find artifact|missing go\.sum entry|Dependabot encountered|Failed to download version"
)
FAILURE_FROM_STEP = {"test": "test", "lint": "lint", "build": "build", "containers": "build", "deps": "deps"}
CONFIG_FAILURE = re.compile(
    r"Unable to resolve action|unable to find version|Invalid workflow file|workflow is not valid"
    r"|Can't find 'action\.ya?ml'|Unrecognized named-value|was deprecated on .+ no longer"
    r"|is no longer (?:updated or )?supported",
    re.I,
)
LINT_FAILURE = re.compile(
    r"\.{4,}Failed|would reformat|would be reformatted|\((?:original|reformatted)\)$|Oh no!|Found \d+ errors? in"
    r"|^\S+\.\w+:\d+:\d+: [A-Z]+\d+ |: [CRWEF]\d{4}: |error: .+\s\[[\w-]+\]$|files were modified by this hook"
    r"|✖ \d+ (?:new )?(?:errors?|(?:lint )?issues?)|level=error"
)
BUILD_FAILURE = re.compile(
    r"BUILD FAILURE|BUILD FAILED|compilation (?:failed|error)|\berror: |SyntaxError|cannot find symbol|make: \*\*\*"
    r"|error TS\d+|ERROR in "
)
# Process gates that fail on purpose: PR titles, changelogs, generated files that drifted, fixup commits.
POLICY_JOB = re.compile(
    r"pr title|semantic|changelog|conventional|commitlint|\bdco\b|sign-?off|label|generated|enforcer|fixup", re.I
)
# Aggregator jobs that only report that another job failed.
UPSTREAM_JOB = re.compile(r"checks? completed|all[- ]checks|ci[- ](?:success|passed|ok)|required[- ]checks", re.I)

STAMP = re.compile(r"^\ufeff?(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z ?")
ESCAPES = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)|[\x00-\x08\x0b-\x1f\x7f]")
MARKER = re.compile(r"^##\[(group|endgroup|error|warning|notice|debug|command)\]")
PERSONAL = re.compile(
    r"author|committer|signed-off-by|co-authored-by|reviewed-by|user\.name|user\.email|mailto:|\bactor\b|triggered by",
    re.I,
)
HANDLE = re.compile(r"(?<![\w.])@[A-Za-z0-9][A-Za-z0-9-]+\b")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b")
TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|xox[abpr]-[\w-]+|AKIA[0-9A-Z]{16})\b")
URL_QUERY = re.compile(r"(https?://[^\s?#]+)[?#]\S*")
WORKDIR = re.compile(r"/home/runner/work/([^/\s]+)/\1/?")
HEAD_AT = re.compile(r"^(HEAD is now at [0-9a-f]{7,40}).*")
MERGE_FROM = re.compile(r"(Merge pull request #\d+) from \S+")
ACTION_REF = re.compile(r"Download action repository '([^'@]+)@([^']+)'")
SHA = re.compile(r"^[0-9a-f]{40}$")
TAG = re.compile(r"^v?\d+(?:\.\d+)*(?:[-+.\w]*)?$")
EXIT_CODE = re.compile(r"Process completed with exit code \d+\.?$")
DEPRECATION = re.compile(
    r"deprecat|Node\.js \d+ actions|node ?1[0-9]\b|set-output|save-state|will be removed|no longer supported", re.I
)
SOURCE_REF = re.compile(r"\.(?:py|java|go|ts|tsx|js|kt|scala|rs|rb|c|cc|cpp|h|yaml|yml)[:\[(]\s*\[?\d+")

PYTEST_SUMMARY = re.compile(r"^=+ (.*?\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed).*?) in ([\d.]+)s")
PYTEST_COUNT = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)")
MAVEN_TOTAL = re.compile(r"^\[(?:INFO|WARNING|ERROR)\] Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)$")
GO_RESULT = re.compile(r"^--- (PASS|FAIL|SKIP): ")
GO_PACKAGE = re.compile(r"^(ok|FAIL)\s+\S+\s+(?:\([^)]*\)|[\d.]+s)")
GOTESTSUM = re.compile(r"^DONE (\d+) tests?(?:, (\d+) skipped)?(?:, (\d+) failures?)?(?:, (\d+) errors?)? in ")
JEST = re.compile(r"^Tests:\s+(?:(\d+) failed, )?(?:(\d+) skipped, )?(?:(\d+) passed, )?(\d+) total")
GRADLE = re.compile(r"(\d+) tests? completed, (\d+) failed(?:, (\d+) skipped)?")
JUNIT_REPORT = re.compile(r"(\d+) PASSED\b.*?(\d+) FAILED\b.*?(\d+) SKIPPED\b")

# Individual test results, at the finest grain each tool prints: pytest -v and Go -v test cases, Maven test classes.
PYTEST_CASE = re.compile(
    r"^(?:\[gw\d+\] \[\s*\d+%\] )?(?:(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS) (\S+::\S+)"
    r"|(\S+::\S+) (PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)\b)"
)
PYTEST_SECTION = re.compile(r"^=+ (short test summary info|test session starts) =+")
# Without -v, pytest prints one progress line per test file ("tests/test_x.py ..F.s  [ 12%]"), wrapping long files.
PYTEST_FILE = re.compile(r"^(\S+\.py) ([.FEsxX]+)\s*(?:\[\s*\d+%\])?$")
PYTEST_MORE = re.compile(r"^([.FEsxX]+)\s*\[\s*\d+%\]$")
TEST_START = re.compile(r"^\[INFO\] Running [\w$.]+$|^T E S T S$|test session starts|^=== RUN")
JS_TEST_FILE = re.compile(
    r"^(?:PASS|FAIL|[\u2713\u2717\u00d7\u276f]) (\S+\.(?:test|spec)\.[cm]?[jt]sx?)(?: \((\d+) tests?\))?"
)
MAVEN_CASE = re.compile(
    r"Tests run: (\d+), Failures: \d+, Errors: \d+, Skipped: (\d+), Time elapsed: [\d.,]+ s(?:ec)?\b.*?-+ ?in ([\w$.]+)"
)
GO_CASE = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)")
GO_PACKAGE_DONE = re.compile(r"^(?:ok|FAIL)\s+(\S+)\s+(?:\([^)]*\)|[\d.]+s)")
GOTESTSUM_CASE = re.compile(r"^(PASS|FAIL|SKIP) (\S+\.Test\S*) \(")
GRADLE_CASE = re.compile(r"^([\w$.]+) > (.+?) (PASSED|FAILED|SKIPPED)$")

CACHE_HIT = re.compile(r"Cache restored successfully|Cache restored from key|cache hit occurred", re.I)
CACHE_MISS = re.compile(r"Cache not found|cache is not found|No cache found|Failed to restore", re.I)
CACHE_SAVE = re.compile(r"Cache saved (?:with key|successfully)")
CACHE_SIZE = re.compile(r"Cache Size: ~?[\d.]+ MB \((\d+) B\)")
DOCKER_PULL = re.compile(r"Pulling from (\S+)|Pulling (?:docker )?image:? ?(\S+)", re.I)
DOWNLOAD_LINE = re.compile(r"Downloaded from |^go: downloading |^\s*Downloading \S|^Get:\d+ ")
TEST_FAILURE = re.compile(
    r"(?:^|\s)(?:FAILED|ERROR) \S+::\S+|\S+::\S+ (?:FAILED|ERROR)\b|^--- FAIL: |<<< (?:FAILURE|ERROR)!|^\s*● "
    r"|^\[ERROR\]\s{2,}[\w$]+\.[\w$]+(?::\d+)?\s|^FAIL\s+\S+\s|^\S+ > .+ FAILED$|There (?:are|were) test failures"
)
STEP_START = re.compile(r"^##\[group\]Run |^Post job cleanup\.|^Cleaning up orphan processes")
MAVEN_CLASS = re.compile(
    r"Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: \d+, Time elapsed: [\d.]+ s <<< FAILURE! -- in "
    r"(?:[\w$]+\.)*([\w$]+)"
)
SIGNATURE_NOISE = re.compile(
    r"exit code \d+|^\[ERROR\]\s*(?:$|->|Re-run|To see|For more|After correcting|\[Help|See |mvn <args>|Errors:\s*$"
    r"|Failures:\s*$)",
    re.I,
)
ERROR_LIKE = re.compile(
    r"\b(?:ERROR|FAILED|FAILURE|FATAL|FAIL)\b|\b[Ee]rror\b|\w+(?:Error|Exception): |\bfatal:|\bpanic:|Exception\b"
    r"|^\S+\.\w+:\d+(?::\d+)?: "
)
GIT_CLEANUP = re.compile(r"^\[command\]/usr/bin/git |^git version |^Temporarily overriding HOME|safe directory|credentials")
JSONISH = re.compile(r'^\s*[\[\]{}"]|"\s?:')
STACK = re.compile(
    r"^\s*at [\w$.<>/]+ ?\(|^\s*\.\.\. \d+ (?:more|common frames)|^\s*File \"|^\s*[~^]+\s*$|^Traceback \(most recent"
)
ACTION_MARK = re.compile(r"^##\[(?:start|end)-action\b")  # newer runners bracket composite action steps like this
IP_ADDRESS = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
PATHISH = re.compile(r"\S*[/\\]\S*|\S+\.\w+:\[?\d+\S*")


@dataclass(slots=True)
class LogLine:
    t: float  # seconds since the log's first line
    text: str  # without the timestamp and terminal escapes


@dataclass(slots=True)
class Step:
    name: str
    start: float  # seconds since the log's first line
    end: float
    result: str
    cmd: str | None = None
    cat: str = "other"
    lines: int = 0


@dataclass
class Choice:
    """A job picked for download."""

    repo: str
    run: dict[str, Any]
    job: dict[str, Any]
    reason: str  # "failed" or "ok"


@dataclass
class Feed:
    rows: list[tuple[float, str, str]] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)

    def add(self, t: float, kind: str, text: str | None, cap: int) -> bool:
        if text is None or not text.strip() or self.counts[kind] >= cap:
            return False
        self.counts[kind] += 1
        self.rows.append((round(t, 2), kind, text))
        return True


def epoch(value: str | None) -> float | None:
    return datetime.fromisoformat(value).timestamp() if value else None


def read_lines(raw: str) -> tuple[list[LogLine], float | None]:
    """Split a job log into lines with times relative to its first line. Returns the lines and the first epoch."""
    seconds: dict[str, float] = {}
    lines: list[LogLine] = []
    first: float | None = None
    now = 0.0
    for line in raw.splitlines():
        match = STAMP.match(line)
        if match:
            whole, fraction = match.group(1), match.group(2)
            base = seconds.get(whole)
            if base is None:
                base = seconds[whole] = datetime.strptime(whole, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC).timestamp()
            stamp = base + (int(fraction[:6].ljust(6, "0")) / 1e6 if fraction else 0.0)
            if first is None:
                first = stamp
            now = max(now, stamp - first)
            text = line[match.end() :]
        else:
            text = line.lstrip("\ufeff")
        lines.append(LogLine(now, ESCAPES.sub("", text)))
    return lines, first


def sanitise(text: str, *, strict: bool = False) -> str | None:
    """A version of a log line that is safe to show, or None when the line could name a person."""
    if PERSONAL.search(text) or (strict and HANDLE.search(text)):
        return None
    text = MARKER.sub("", text).replace("[command]", "$ ", 1).replace("\t", "  ").rstrip()
    text = HEAD_AT.sub(r"\1", text)
    text = MERGE_FROM.sub(r"\1", text)
    text = TOKEN.sub("<token>", text)
    text = EMAIL.sub("<email>", text)
    text = URL_QUERY.sub(r"\1?…", text)
    text = IP_ADDRESS.sub("<ip>", text)
    text = WORKDIR.sub("./", text).replace("/home/runner/", "~/")
    if len(text) > TEXT_LIMIT:
        text = text[: TEXT_LIMIT - 1] + "…"
    return text


def signature_key(text: str) -> str:
    """Groups error lines that differ only by numbers, hashes or quoted values."""
    key = re.sub(r"[0-9a-f]{7,}", "#", text.lower())
    key = re.sub(r"\d+", "n", key)
    key = re.sub(r"'[^']*'|\"[^\"]*\"", "'…'", key)
    return re.sub(r"\s+", " ", key).strip()[:90]


def classify_step(name: str, cmd: str | None) -> str:
    text = f"{name} {cmd or ''}".lower()
    for activity, pattern in STEP_RULES:
        if pattern.search(text):
            return activity
    return "other"


def line_activity(text: str) -> str | None:
    for activity, pattern in LINE_RULES:
        if pattern.search(text):
            return activity
    return None


def build_steps(api_steps: Sequence[dict[str, Any]], first: float) -> list[Step]:
    steps: list[Step] = []
    for s in sorted(api_steps, key=lambda s: int(s.get("number") or 0)):
        start, end = epoch(s.get("started_at")), epoch(s.get("completed_at"))
        if start is None or end is None or s.get("conclusion") == "skipped":
            continue
        steps.append(
            Step(
                str(s.get("name") or "step"),
                max(0.0, start - first),
                max(0.0, end - first + 1),
                str(s.get("conclusion") or ""),
            )
        )
    return steps or [Step("job", 0.0, math.inf, "")]


def runner_header(lines: Sequence[LogLine]) -> dict[str, Any]:
    """Runner version, OS, image and token permissions from the 'Set up job' block at the top of every job log."""
    info: dict[str, Any] = {"os": None, "image": None, "image_version": None, "agent": None}
    token: dict[str, list[str]] = {"write": [], "read": []}
    actions: list[dict[str, str]] = []
    group: str | None = None
    os_parts: list[str] = []
    for line in lines[:400]:
        text = line.text.strip()
        marker = MARKER.match(text)
        if marker and marker.group(1) == "group":
            group = text[marker.end() :].strip()
            continue
        if marker and marker.group(1) == "endgroup":
            group = None
            continue
        if text.startswith("Current runner version:"):
            info["agent"] = text.split(":", 1)[1].strip().strip("'")
        elif group == "Operating System":
            os_parts.append(text)
        elif group == "Runner Image" and text.startswith("Image:"):
            info["image"] = text.split(":", 1)[1].strip()
        elif group == "Runner Image" and text.startswith("Version:"):
            info["image_version"] = text.split(":", 1)[1].strip()
        elif group == "GITHUB_TOKEN Permissions" and ":" in text:
            scope, level = (part.strip() for part in text.split(":", 1))
            if level in ("write", "read"):
                token[level].append(scope)
        elif match := ACTION_REF.search(text):
            ref = match.group(2)
            kind = "sha" if SHA.match(ref) else "tag" if TAG.match(ref) else "branch"
            actions.append({"name": match.group(1), "ref": kind})
        elif text.startswith("##[group]Run ") or text.startswith("Complete job name"):
            break
    info["os"] = " ".join(os_parts) or None
    return {"runner": info, "token": token, "actions": actions}


def test_summary(lines: Sequence[LogLine]) -> dict[str, Any] | None:
    pytest: Counter[str] = Counter()
    maven: Counter[str] = Counter()
    go_cases: Counter[str] = Counter()
    go_packages: Counter[str] = Counter()
    other: dict[str, Any] | None = None
    gradle: Counter[str] = Counter()
    report: dict[str, Any] | None = None
    for line in lines:
        text = MARKER.sub("", line.text).strip()
        if match := PYTEST_SUMMARY.match(text):
            for count, word in PYTEST_COUNT.findall(match.group(1)):
                pytest["error" if word.startswith("error") else word] += int(count)
        elif match := MAVEN_TOTAL.match(text):
            run, failures, errors, skipped = (int(g) for g in match.groups())
            maven.update({"total": run, "failed": failures, "error": errors, "skipped": skipped})
        elif match := GO_RESULT.match(text):
            go_cases[match.group(1)] += 1
        elif match := GO_PACKAGE.match(text):
            go_packages[match.group(1)] += 1
        elif match := GOTESTSUM.match(text):
            total, skipped, failures, errors = (int(g or 0) for g in match.groups())
            other = {"kind": "gotestsum", "total": total, "failed": failures + errors, "skipped": skipped}
        elif match := JEST.match(text):
            failed, skipped, passed, total = (int(g or 0) for g in match.groups())
            other = {"kind": "jest", "total": total, "failed": failed, "skipped": skipped, "passed": passed}
        elif match := JUNIT_REPORT.search(text):
            passed, failed, skipped = (int(g) for g in match.groups())
            report = {
                "kind": "junit",
                "total": passed + failed + skipped,
                "failed": failed,
                "skipped": skipped,
                "passed": passed,
            }
        elif match := GRADLE.search(text):
            gradle.update({"total": int(match.group(1)), "failed": int(match.group(2)), "skipped": int(match.group(3) or 0)})
    if report:
        return report
    if pytest:
        failed = pytest["failed"] + pytest["error"]
        total = failed + pytest["passed"] + pytest["skipped"] + pytest["xfailed"] + pytest["xpassed"]
        return {"kind": "pytest", "total": total, "failed": failed, "skipped": pytest["skipped"], "passed": pytest["passed"]}
    if maven:
        failed = maven["failed"] + maven["error"]
        passed = maven["total"] - failed - maven["skipped"]
        return {"kind": "maven", "total": maven["total"], "failed": failed, "skipped": maven["skipped"], "passed": passed}
    if other:
        other.setdefault("passed", other["total"] - other["failed"] - other["skipped"])
        return other
    if gradle:
        passed = gradle["total"] - gradle["failed"] - gradle["skipped"]
        return {
            "kind": "gradle",
            "total": gradle["total"],
            "failed": gradle["failed"],
            "skipped": gradle["skipped"],
            "passed": passed,
        }
    if go_cases:
        total = sum(go_cases.values())
        return {
            "kind": "go",
            "total": total,
            "failed": go_cases["FAIL"],
            "skipped": go_cases["SKIP"],
            "passed": go_cases["PASS"],
        }
    if go_packages:
        total = sum(go_packages.values())
        return {
            "kind": "go-packages",
            "total": total,
            "failed": go_packages["FAIL"],
            "skipped": 0,
            "passed": go_packages["ok"],
        }
    return None


@dataclass
class TestUnits:
    """The tests one job ran: how often each ran, how many individual tests that was, and which were skipped."""

    __test__ = False  # not a pytest test class, despite the name

    kind: str | None = None
    runs: Counter[str] = field(default_factory=Counter)
    weight: dict[str, int] = field(default_factory=dict)  # tests inside one unit (a Maven class holds several)
    skipped: set[str] = field(default_factory=set)
    first_t: float | None = None  # when the first and last test lines were printed
    last_t: float | None = None

    def seen(self, t: float) -> None:
        self.first_t = t if self.first_t is None else self.first_t
        self.last_t = t

    @property
    def span(self) -> float:
        return self.last_t - self.first_t if self.first_t is not None and self.last_t is not None else 0.0

    def add(self, kind: str, unit: str, tests: int = 1, skipped: bool = False) -> None:
        self.kind = self.kind or kind
        self.runs[unit] += 1
        self.weight[unit] = max(tests, self.weight.get(unit, 1))
        if skipped:
            self.skipped.add(unit)

    @property
    def tests(self) -> int:
        return sum(self.weight[u] * n for u, n in self.runs.items())

    def summary(self) -> dict[str, Any] | None:
        if not self.runs:
            return None
        return {
            "kind": self.kind,
            "units": len(self.runs),
            "tests": self.tests,
            "repeated": sum(n - 1 for u, n in self.runs.items() if n > 1 and "***" not in u),
            "skipped": len(self.skipped),
            "test_span_min": round(self.span / 60, 1),
        }


def test_units(lines: Sequence[LogLine]) -> TestUnits:
    """The tests a job ran, at the finest grain its log shows: a test case when the tool prints one line per test
    (pytest -v, go test -v, Gradle with test logging), otherwise a test file (pytest, Jest, Vitest), a Maven test
    class or a Go package."""
    found = TestUnits()
    in_summary = False
    pending_go: list[tuple[str, str]] = []
    pytest_file: str | None = None  # the file whose progress dots may continue on the next line
    for line in lines:
        text = MARKER.sub("", line.text).strip()
        before = len(found.runs), sum(found.runs.values())
        if TEST_START.search(text):
            found.seen(line.t)
        if section := PYTEST_SECTION.match(text):
            # pytest -rA lists every result again at the end. Those lines are a summary, not more test runs.
            in_summary = section.group(1).startswith("short")
            pytest_file = None
        elif in_summary:
            continue
        elif match := PYTEST_CASE.match(text):
            status, node = (match.group(1), match.group(2)) if match.group(1) else (match.group(4), match.group(3))
            found.add("pytest", node, skipped=status == "SKIPPED")
        elif match := PYTEST_FILE.match(text):
            pytest_file, marks = match.group(1), match.group(2)
            found.add("pytest-file", pytest_file, tests=len(marks), skipped=set(marks) == {"s"})
        elif pytest_file and (match := PYTEST_MORE.match(text)):
            found.weight[pytest_file] += len(match.group(1))
        elif match := MAVEN_CASE.search(text):
            run, skipped, name = int(match.group(1)), int(match.group(2)), match.group(3)
            found.add("maven", name, tests=run, skipped=run > 0 and skipped == run)
        elif match := GO_CASE.match(text):
            pending_go.append((match.group(2), match.group(1)))
        elif match := GO_PACKAGE_DONE.match(text):
            for name, status in pending_go:
                found.add("go", f"{match.group(1)}.{name}", skipped=status == "SKIP")
            if not pending_go:
                found.add("go-package", match.group(1))
            pending_go = []
        elif match := GOTESTSUM_CASE.match(text):
            found.add("go", match.group(2), skipped=match.group(1) == "SKIP")
        elif match := GRADLE_CASE.match(text):
            found.add("gradle", f"{match.group(1)}.{match.group(2)}", skipped=match.group(3) == "SKIPPED")
        elif match := JS_TEST_FILE.match(text):
            found.add("js-file", match.group(1), tests=int(match.group(2) or 1))
        else:
            pytest_file = None
        if (len(found.runs), sum(found.runs.values())) != before:
            found.seen(line.t)
    for name, status in pending_go:
        found.add("go", f"?.{name}", skipped=status == "SKIP")
    return found


def failure_reason(
    lines: Sequence[LogLine], owners: Sequence[int], echo: Sequence[bool], steps: Sequence[Step], job_name: str
) -> dict[str, Any] | None:
    """Sort a failed job into one class, reading only what the failing step printed (not the inputs it echoed)."""
    failing = [i for i, s in enumerate(steps) if s.result == "failure"]
    if not failing:
        return None
    index = failing[0]
    step = steps[index]
    output = [ln for ln, owner, quoted in zip(lines, owners, echo, strict=True) if owner == index and not quoted]
    texts = [MARKER.sub("", ln.text).strip() for ln in output]
    errors = [ln.text[len("##[error]") :] for ln in output if ln.text.startswith("##[error]")]
    near = "\n".join([*errors, *texts[-80:]])
    name = f"{job_name} {step.name}"
    if TIMEOUT_FAILURE.search(near):
        category = "timeout"
    elif CONFIG_FAILURE.search(near):
        category = "config"
    elif any(TEST_FAILURE.search(t) for t in texts):
        category = "test"
    elif any(LINT_FAILURE.search(t) for t in texts):
        category = "lint"
    elif INFRA_FAILURE.search(near):
        category = "infra"
    elif DEPS_FAILURE.search(near) or "dependabot" in name.lower():
        category = "deps"
    elif UPSTREAM_JOB.search(job_name):
        category = "upstream"
    elif POLICY_JOB.search(name):
        category = "policy"
    elif step.cat in ("lint", "test", "deps"):
        category = FAILURE_FROM_STEP[step.cat]
    elif any(BUILD_FAILURE.search(t) for t in texts[-200:]):
        category = "build"
    else:
        category = FAILURE_FROM_STEP.get(step.cat, "other")
    return {"class": category, "step": step.name, "signature": failure_signature(output, category)}


def failure_signature(output: Sequence[LogLine], category: str) -> str | None:
    """The most telling line of a failed step: a failing test or lint finding, an annotated error, an error line,
    then simply the last thing the step printed."""
    texts = [MARKER.sub("", ln.text).strip() for ln in output]
    picks: list[str] = []
    if category == "test":
        picks += [t for t in texts if TEST_FAILURE.search(t)][:1]
    if category == "lint":
        picks += [t for t in texts if LINT_FAILURE.search(t)][:1]
    picks += [ln.text[len("##[error]") :].strip() for ln in output if ln.text.startswith("##[error]")]
    picks += [t for t in reversed(texts[-300:]) if ERROR_LIKE.search(t)]
    picks += [t for t in reversed(texts[-40:]) if meaningful(t) and not GIT_CLEANUP.search(t)]
    for text in picks:
        if SIGNATURE_NOISE.search(text) or STACK.search(text) or ACTION_MARK.match(text):
            continue
        if match := MAVEN_CLASS.search(text):
            run, failures, errors, test_class = match.groups()
            text = f"{test_class}: {int(failures) + int(errors)} of {run} tests failed"
        if clean := sanitise(text):
            return clean
    return None


def analyse(raw: str, meta: dict[str, Any]) -> dict[str, Any] | None:
    lines, first = read_lines(raw)
    if not lines or first is None:
        return None
    steps = build_steps(meta["steps"], first)
    span = max(1.0, lines[-1].t)
    n_bins = int(span) + 1

    # Which step printed each line. GitHub reports step start times cut to the whole second, so a step only takes over
    # from the one before at its own "Run ..." or cleanup marker, or once a full second has passed. Otherwise the last
    # lines of a failing step, the ones that say why it failed, would land in the next step. `echo` marks the lines
    # where a step repeats its own command, inputs and environment rather than printing output.
    owners: list[int] = []
    echo: list[bool] = []
    index = 0
    in_header = False
    for line in lines:
        moved = False
        while index + 1 < len(steps) and steps[index + 1].start <= line.t:
            if line.t >= steps[index + 1].start + 1.0:
                index += 1
            elif not moved and STEP_START.match(line.text):
                index += 1
                moved = True
            else:
                break
        owners.append(index)
        step = steps[index]
        step.lines += 1
        if line.text.startswith("##[group]Run "):
            in_header = True
            if step.cmd is None:
                step.cmd = line.text[len("##[group]Run ") :].strip()
            echo.append(True)
        elif in_header and line.text.startswith("##[endgroup]"):
            in_header = False
            echo.append(True)
        else:
            echo.append(in_header)
    for step in steps:
        step.cat = classify_step(step.name, step.cmd)

    # One-second bins: line counts and the activity with most lines. A silent second keeps the last activity seen in
    # the same step, since tools tend to print what they start ("Compiling 363 source files") and then go quiet.
    counts = [0] * n_bins
    votes: list[Counter[str] | None] = [None] * n_bins
    for line, owner in zip(lines, owners, strict=True):
        b = min(n_bins - 1, int(line.t))
        counts[b] += 1
        activity = line_activity(MARKER.sub("", line.text)) or steps[owner].cat
        bucket = votes[b]
        if bucket is None:
            bucket = votes[b] = Counter()
        bucket[activity] += 1
    step_at = [0] * n_bins
    index = 0
    for b in range(n_bins):
        while index + 1 < len(steps) and steps[index + 1].start <= b:
            index += 1
        step_at[b] = index
    activity_at: list[str] = []
    last: tuple[str, int, int] | None = None  # activity, bin, step
    for b in range(n_bins):
        own = steps[step_at[b]].cat
        bucket = votes[b]
        if bucket:
            chosen = max(bucket.items(), key=lambda kv: (kv[1], kv[0] == own))[0]
            last = (chosen, b, step_at[b])
        elif last and last[2] == step_at[b]:
            chosen = last[0]
        else:
            chosen = own
        activity_at.append(chosen)
    seconds = Counter(activity_at)

    # Silent stretches: what was printed just before and just after.
    gaps: list[dict[str, Any]] = []
    quiet = 0.0
    for i in range(len(lines) - 1):
        dt = lines[i + 1].t - lines[i].t
        if dt < GAP_S:
            continue
        quiet += dt
        before = next((sanitise(lines[j].text) for j in range(i, max(-1, i - 6), -1) if meaningful(lines[j].text)), None)
        after = next(
            (sanitise(lines[j].text) for j in range(i + 1, min(len(lines), i + 6)) if meaningful(lines[j].text)), None
        )
        gaps.append(
            {"t": round(lines[i].t, 1), "d": round(dt, 1), "step": steps[owners[i]].name, "before": before, "after": after}
        )
    gaps.sort(key=lambda g: -float(g["d"]))
    top_gaps = sorted(gaps[:12], key=lambda g: float(g["t"]))

    # Trace for the oscilloscope: line counts and the main activity per bucket.
    width = max(1, math.ceil(n_bins / TRACE_BUCKETS))
    trace = [sum(counts[k : k + width]) for k in range(0, n_bins, width)]
    band = "".join(BAND_CODE[Counter(activity_at[k : k + width]).most_common(1)[0][0]] for k in range(0, n_bins, width))

    header = runner_header(lines)
    feed = Feed()
    for key, value in (
        ("Runner image", f"{header['runner']['image']} · {header['runner']['image_version']}"),
        ("Operating system", header["runner"]["os"]),
        ("Runner version", header["runner"]["agent"]),
    ):
        if value and "None" not in str(value):
            feed.add(0.0, "sys", f"{key}: {value}", 6)
    if header["token"]["write"] or header["token"]["read"]:
        write = ", ".join(header["token"]["write"]) or "none"
        feed.add(0.0, "sys", f"GITHUB_TOKEN write: {write} · read: {len(header['token']['read'])} scopes", 6)

    errors = warnings = 0
    warn_kinds: Counter[str] = Counter()
    deprecations: Counter[str] = Counter()
    cache = {"hit": 0, "miss": 0, "saved": 0, "restored_mb": 0.0}
    last_hit = -10.0
    pulls: list[str] = []
    downloads = 0
    exit_line: tuple[float, str] | None = None
    ambient: list[int] = []
    seen_warnings: set[str] = set()
    in_run_block = False
    for i, line in enumerate(lines):
        text = line.text
        marker = MARKER.match(text)
        kind = marker.group(1) if marker else None
        body = text[marker.end() :] if marker else text
        if kind == "group":
            in_run_block = body.startswith("Run ")
            if in_run_block:
                feed.add(line.t, "cmd", sanitise(body), 70)
            continue
        if kind == "endgroup":
            in_run_block = False
            continue
        if kind == "error":
            errors += 1
            if EXIT_CODE.search(body):
                exit_line = (line.t, body)
            else:
                feed.add(line.t, "err", sanitise(body), 30)
            continue
        if kind == "warning":
            warnings += 1
            # A warning about a source file is the code's own (compiler, linter); the rest are about CI itself.
            if SOURCE_REF.search(body):
                warn_kinds["code"] += 1
            elif DEPRECATION.search(body):
                warn_kinds["deprecation"] += 1
                clean = sanitise(re.sub(r"\S+@\S+|[\w./-]+\.(?:yml|yaml)", "…", body))
                if clean:
                    deprecations[clean[:120]] += 1
            else:
                warn_kinds["other"] += 1
            key = signature_key(PATHISH.sub("", body))[:60]
            if key not in seen_warnings:
                seen_warnings.add(key)
                feed.add(line.t, "warn", sanitise(body), 10)
            continue
        if CACHE_HIT.search(body):
            if line.t - last_hit > 2:
                cache["hit"] += 1
            last_hit = line.t
            feed.add(line.t, "cache", sanitise(body), 12)
        elif CACHE_MISS.search(body):
            cache["miss"] += 1
            feed.add(line.t, "cache", sanitise(body), 12)
        elif CACHE_SAVE.search(body):
            cache["saved"] += 1
            feed.add(line.t, "cache", sanitise(body), 12)
        elif match := CACHE_SIZE.search(body):
            cache["restored_mb"] += int(match.group(1)) / 1e6
        elif match := DOCKER_PULL.search(body):
            image = match.group(1) or match.group(2)
            if image and image not in pulls:
                pulls.append(image)
                feed.add(line.t, "dock", sanitise(body), 10)
        elif PYTEST_SUMMARY.match(body.strip()) or MAVEN_TOTAL.match(body.strip()) or GOTESTSUM.match(body.strip()):
            feed.add(line.t, "test", sanitise(body), 14)
        elif re.search(r"^(?:FAILED |ERROR )\S+::|^--- FAIL: |<<< (?:FAILURE|ERROR)!", body.strip()):
            feed.add(line.t, "fail", sanitise(body.strip()), 12)
        elif (
            not in_run_block
            and meaningful(body)
            and not JSONISH.search(body)
            and not STACK.search(body)
            and steps[owners[i]].name != "Set up job"
        ):
            ambient.append(i)
        if DOWNLOAD_LINE.search(body):
            downloads += 1
    for gap in top_gaps:
        feed.add(float(gap["t"]) + 0.01, "gap", f"{gap['d']:.0f}s without output · after: {gap['before'] or '—'}", 12)
    if exit_line:
        feed.add(exit_line[0], "err", sanitise(exit_line[1]), 31)

    # Fill the rest of the feed with ordinary output: the first line in each equal slice of the job's time, so a burst
    # (a stack trace, a dependency download) cannot crowd out the rest.
    room = FEED_LIMIT - len(feed.rows)
    if room > 0 and ambient:
        slot = span / room
        taken: set[int] = set()
        for i in ambient:
            position = int(lines[i].t / slot)
            if position not in taken:
                taken.add(position)
                feed.add(lines[i].t, "out", sanitise(lines[i].text, strict=True), FEED_LIMIT)
    feed.rows.sort(key=lambda row: row[0])

    result = str(meta["job"].get("conclusion") or "")
    api_start, api_end = epoch(meta["job"].get("started_at")), epoch(meta["job"].get("completed_at"))
    created = epoch(meta["job"].get("created_at"))
    return {
        "id": meta["job"]["id"],
        "run_id": meta["run"]["id"],
        "repo": meta["repo"],
        "workflow": meta["run"].get("workflow"),
        "event": meta["run"].get("event"),
        "job": meta["job"].get("name"),
        "result": result,
        "picked": meta["reason"],
        "start": meta["job"].get("started_at"),
        "dur": round(api_end - api_start, 1) if api_start and api_end else round(span, 1),
        "queue": round(api_start - created, 1) if api_start and created else None,
        "lines": len(lines),
        "bytes": len(raw.encode("utf-8", "replace")),
        "labels": meta["job"].get("labels") or [],
        **header,
        "steps": [
            {
                "name": s.name,
                "t": round(s.start, 1),
                "d": round(max(0.0, min(s.end, span + 1) - s.start), 1),
                "result": s.result,
                "cat": s.cat,
                "cmd": sanitise(s.cmd) if s.cmd else None,
                "lines": s.lines,
            }
            for s in steps
            if math.isfinite(s.end)
        ],
        "act": {a: seconds.get(a, 0) for a in ACTIVITIES},
        "quiet": round(quiet, 1),
        "gaps": top_gaps,
        "bucket": width,
        "trace": trace,
        "band": band,
        "errors": errors,
        "warnings": warnings,
        "warn_kinds": dict(warn_kinds),
        "deprecations": [{"text": k, "n": v} for k, v in deprecations.most_common(5)],
        "cache": {**cache, "restored_mb": round(cache["restored_mb"], 1)},
        "pulls": len(pulls),
        "downloads": downloads,
        "tests": test_summary(lines),
        "test_units": test_units(lines).summary(),
        "failure": failure_reason(lines, owners, echo, steps, str(meta["job"].get("name") or ""))
        if result == "failure"
        else None,
        "feed": [list(row) for row in feed.rows],
    }


def meaningful(text: str) -> bool:
    if ACTION_MARK.match(text):
        return False
    body = MARKER.sub("", text).strip()
    return len(body) > 3 and not body.startswith(("shell:", "env:", "with:")) and not re.fullmatch(r"[\W_]+", body)


def pick_runs(rows: Sequence[dict[str, Any]], failed: int, ok: int) -> dict[str, list[tuple[dict[str, Any], str]]]:
    """The latest failed and successful runs per repository, one per workflow first, developer events first."""
    by_repo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_repo[row["repo"]].append(row)
    chosen: dict[str, list[tuple[dict[str, Any], str]]] = {}
    for repo, runs in by_repo.items():
        runs.sort(key=lambda r: str(r["ts"]), reverse=True)
        picks: list[tuple[dict[str, Any], str]] = []
        for result, reason, want in (("failure", "failed", failed), ("success", "ok", ok)):
            pool = [r for r in runs if r["result"] == result and r["event"] in DEV_EVENTS]
            pool = pool or [r for r in runs if r["result"] == result]
            if result == "success":
                frequency = Counter(r["workflow"] for r in pool)
                pool.sort(key=lambda r: (-frequency[r["workflow"]], -datetime.fromisoformat(r["ts"]).timestamp()))
            taken: list[dict[str, Any]] = []
            seen: set[str] = set()
            for run in pool:
                if len(taken) < want and run["workflow"] not in seen:
                    taken.append(run)
                    seen.add(run["workflow"])
            for run in pool:
                if len(taken) < want and run not in taken:
                    taken.append(run)
            picks += [(run, reason) for run in taken]
        chosen[repo] = picks
    return chosen


def pick_jobs(jobs: Sequence[dict[str, Any]], reason: str, per_failed_run: int) -> list[dict[str, Any]]:
    def length(job: dict[str, Any]) -> float:
        start, end = epoch(job.get("started_at")), epoch(job.get("completed_at"))
        return end - start if start and end else 0.0

    usable = [j for j in jobs if j.get("steps") and j.get("started_at") and j.get("completed_at")]
    if reason == "failed":
        failed = [j for j in usable if j.get("conclusion") == "failure"]
        picked: list[dict[str, Any]] = []
        names: set[str] = set()
        for job in sorted(failed, key=length, reverse=True):
            base = re.sub(r"\s*\([^)]*\)\s*$", "", str(job.get("name")))
            if base not in names and len(picked) < per_failed_run:
                picked.append(job)
                names.add(base)
        return picked
    ok = [j for j in usable if j.get("conclusion") == "success"]
    return sorted(ok, key=length, reverse=True)[:1]


def cache_dir_for(root: Path, repo: str) -> Path:
    path = root.joinpath(*repo.split("/"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_jobs(gh: Gh | None, root: Path, repo: str, run_id: int) -> list[dict[str, Any]] | None:
    path = cache_dir_for(root, repo) / f"run-{run_id}-jobs.json"
    if path.exists():
        cached: list[dict[str, Any]] = json.loads(path.read_text())
        return cached
    if gh is None:
        return None
    body = gh.json(f"repos/{repo}/actions/runs/{run_id}/jobs", {"per_page": 100, "filter": "latest"}) or {}
    keep = ("id", "name", "conclusion", "created_at", "started_at", "completed_at", "labels", "steps")
    jobs = [{k: job.get(k) for k in keep} for job in body.get("jobs", [])]
    for job in jobs:
        job["steps"] = [
            {k: s.get(k) for k in ("name", "number", "conclusion", "started_at", "completed_at")} for s in job["steps"] or []
        ]
    path.write_text(json.dumps(jobs))
    return jobs


def fetch_log(gh: Gh | None, root: Path, repo: str, job_id: int) -> str | None:
    path = cache_dir_for(root, repo) / f"job-{job_id}.log.gz"
    if path.exists():
        return gzip.decompress(path.read_bytes()).decode("utf-8", "replace")
    if gh is None:
        return None
    data = gh.raw(f"repos/{repo}/actions/jobs/{job_id}/logs")
    path.write_bytes(gzip.compress(data, 6))
    return data.decode("utf-8", "replace")


# ---------------------------------------------------------------------------------------------------------------------
# Test redundancy audit: every job that ran on one recent pull request commit, and which tests ran more than once
# ---------------------------------------------------------------------------------------------------------------------


SCAN_JOB = re.compile(r"codeql|analy[sz]e|code scanning|security|sonar|semgrep|trivy", re.I)
TEST_JOB = re.compile(r"test|e2e|junit|integration|spec", re.I)


def base_job(name: str) -> str:
    """Legs of one matrix job share this: 'test (3.13, ubuntu-latest)' and 'test (3.12, macos-latest)', or
    'JUnit tests Java 17' and 'JUnit tests Java 25'. Only standalone version numbers are dropped, so 'S3' stays."""
    name = re.sub(r"\s*\([^)]*\)\s*$", "", name)
    name = re.sub(r"\b\d+(?:\.\d+)*\b", "#", name)
    return re.sub(r"\s+", " ", name).strip()


@dataclass
class AuditJob:
    workflow: str
    job: str
    event: str
    dur: float
    result: str
    units: TestUnits

    @property
    def label(self) -> str:
        return f"{self.workflow} / {self.job}" + (f" [{self.event}]" if self.event != "pull_request" else "")

    @property
    def scan(self) -> bool:
        """A security scan (CodeQL and the like) only needs the code built. Tests run there are pure overhead."""
        return bool(SCAN_JOB.search(f"{self.workflow} {self.job}"))


def choose_audit_commit(gh: Gh | None, root: Path, repo: str) -> dict[str, Any] | None:
    """The newest pull request commit whose checks all passed on at least two workflows, with every run on it."""
    path = cache_dir_for(root, repo) / "audit-commit.json"
    if path.exists():
        cached: dict[str, Any] = json.loads(path.read_text())
        return cached
    if gh is None:
        return None
    body = gh.json(f"repos/{repo}/actions/runs", {"event": "pull_request", "status": "completed", "per_page": 100}) or {}
    by_sha: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in body.get("workflow_runs", []):
        by_sha[run["head_sha"]].append(run)
    newest_first = sorted(by_sha.items(), key=lambda kv: max(str(r["created_at"]) for r in kv[1]), reverse=True)

    def clean(runs: list[dict[str, Any]]) -> bool:
        results = [r["conclusion"] for r in runs]
        return "failure" not in results and results.count("success") >= 2

    pick = next((kv for kv in newest_first if clean(kv[1])), None)
    pick = pick or next((kv for kv in newest_first if any(r["conclusion"] == "success" for r in kv[1])), None)
    if pick is None:
        return None
    sha, pr_runs = pick
    every = gh.json(f"repos/{repo}/actions/runs", {"head_sha": sha, "per_page": 100}) or {}
    keep = ("id", "name", "event", "conclusion", "created_at", "run_started_at", "updated_at", "run_attempt", "path")
    prs = pr_runs[0].get("pull_requests") or []
    choice = {
        "sha": sha,
        "pr": prs[0]["number"] if prs else None,
        "runs": [{k: r.get(k) for k in keep} for r in every.get("workflow_runs", [])],
    }
    path.write_text(json.dumps(choice))
    return choice


def job_seconds(job: dict[str, Any]) -> float:
    start, end = epoch(job.get("started_at")), epoch(job.get("completed_at"))
    return end - start if start and end else 0.0


def audit_repo(gh: Gh | None, root: Path, repo: str, max_jobs: int) -> dict[str, Any] | None:
    choice = choose_audit_commit(gh, root, repo)
    if not choice:
        return None
    candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for run in choice["runs"]:
        if run["conclusion"] not in ("success", "failure"):
            continue
        for job in list_jobs(gh, root, repo, int(run["id"])) or []:
            if job.get("conclusion") in ("success", "failure") and job_seconds(job) >= 10:
                candidates.append((run, job))
    candidates.sort(key=lambda pair: -job_seconds(pair[1]))
    jobs: list[AuditJob] = []
    missing = 0
    for run, job in candidates[:max_jobs]:
        try:
            raw = fetch_log(gh, root, repo, int(job["id"]))
        except GhError:
            raw = None
        if raw is None:
            missing += 1
            continue
        lines, _ = read_lines(raw)
        units = test_units(lines)
        if not units.runs and (summary := test_summary(lines)) and summary["total"]:
            # Only a total was printed, so compare whole suites: same framework and the same number of tests.
            units.add("suite", f"suite:{summary['kind']}:{summary['total']}", tests=int(summary["total"]))
        jobs.append(
            AuditJob(str(run["name"]), str(job["name"]), str(run["event"]), job_seconds(job), str(job["conclusion"]), units)
        )
    found = redundancy(repo, choice, jobs, skipped_jobs=max(0, len(candidates) - max_jobs))
    found["logs_missing"] = missing
    return found


def matrix_name(base: str) -> str:
    """'JUnit tests Java #' reads as 'JUnit tests Java'."""
    return re.sub(r"\s*#", "", base).strip() or base


def matrix_variant(name: str) -> str:
    """What sets one leg apart: its parenthesised suffix, or the version numbers in its name."""
    suffix = re.search(r"\(([^)]*)\)\s*$", name)
    if suffix:
        return suffix.group(1)
    return " ".join(re.findall(r"\b\d+(?:\.\d+)*\b", name)) or name


def relation(a: AuditJob, b: AuditJob) -> str:
    if a.scan or b.scan:
        return "security-scan"
    if a.workflow != b.workflow:
        return "cross-workflow"
    if a.event != b.event:
        return "double-trigger"
    if base_job(a.job) == base_job(b.job):
        return "matrix"
    return "same-workflow"


def redundancy(repo: str, choice: dict[str, Any], jobs: Sequence[AuditJob], skipped_jobs: int) -> dict[str, Any]:
    """How many times each test ran on one commit, and why. Repeats are put in one of six buckets: inside a security
    scan (never needed), another workflow running the same tests, the same workflow fired by both push and
    pull_request, another job of the same workflow, matrix legs (deliberate coverage, but a cost), or the same test
    twice inside one job."""
    testers = [j for j in jobs if j.units.runs]
    weight: dict[str, int] = {}
    for job in testers:
        for unit, tests in job.units.weight.items():
            weight[unit] = max(tests, weight.get(unit, 1))
    extra = Counter[str]()
    seen_in: dict[str, list[AuditJob]] = defaultdict(list)
    repeated_examples: list[str] = []
    for job in testers:
        for unit, count in job.units.runs.items():
            seen_in[unit].append(job)
            # A test id with *** in it was partly masked as a secret, so two different ids can look the same.
            if count > 1 and "***" not in unit:
                extra["within_job"] += (count - 1) * weight[unit]
                if unit not in repeated_examples and len(repeated_examples) < 4:
                    repeated_examples.append(unit)
    for unit, holders in seen_in.items():
        w = weight[unit]
        others = [h for h in holders if not h.scan]
        extra["in_security_scan"] += (len(holders) - len(others)) * w
        if others:
            workflows = {h.workflow for h in others}
            events = {(h.workflow, h.event) for h in others}
            legs = {(h.workflow, h.event, base_job(h.job)) for h in others}
            extra["cross_workflow"] += (len(workflows) - 1) * w
            extra["double_trigger"] += (len(events) - len(workflows)) * w
            extra["same_workflow"] += (len(legs) - len(events)) * w
            extra["matrix"] += (len(others) - len(legs)) * w
    always_skipped = [unit for unit, holders in seen_in.items() if all(unit in h.units.skipped for h in holders)]

    pairs: list[dict[str, Any]] = []
    redundant: dict[int, dict[str, Any]] = {}
    for index, job in enumerate(testers):
        if job.scan:
            minutes = job.units.span / 60
            redundant[index] = {
                "job": job.label,
                "covered_by": None,
                "relation": "security-scan",
                "minutes": round(minutes, 1),
            }
    for (ia, a), (ib, b) in combinations(enumerate(testers), 2):
        shared = set(a.units.runs) & set(b.units.runs)
        if not shared:
            continue
        overlap = len(shared) / min(len(a.units.runs), len(b.units.runs))
        kind = relation(a, b)
        pairs.append({"a": a.label, "b": b.label, "relation": kind, "shared": len(shared), "overlap": round(overlap, 3)})
        if kind in ("cross-workflow", "double-trigger", "same-workflow") and overlap >= 0.8:
            # The shorter job of the two is the one that adds nothing new.
            victim, other = (ia, b) if a.dur <= b.dur else (ib, a)
            redundant.setdefault(
                victim,
                {
                    "job": testers[victim].label,
                    "covered_by": other.label,
                    "relation": kind,
                    "minutes": round(testers[victim].dur / 60, 1),
                },
            )
    order = {"security-scan": 0, "double-trigger": 1, "cross-workflow": 2, "same-workflow": 3, "matrix": 4}
    pairs.sort(key=lambda p: (order[str(p["relation"])], -int(p["shared"])))

    # Matrix legs of one job: deliberate, but every leg after the longest is extra compute on each commit.
    groups: dict[tuple[str, str, str], list[AuditJob]] = defaultdict(list)
    for job in testers:
        if not job.scan:
            groups[(job.workflow, job.event, base_job(job.job))].append(job)
    matrix: list[dict[str, Any]] = [
        {
            "job": f"{workflow} / {matrix_name(name)}",
            "legs": len(legs),
            "minutes": round(sum(j.dur for j in legs) / 60, 1),
            "extra_minutes": round((sum(j.dur for j in legs) - max(j.dur for j in legs)) / 60, 1),
            "variants": [matrix_variant(j.job) for j in legs],
        }
        for (workflow, _event, name), legs in groups.items()
        if len(legs) > 1
    ]
    matrix.sort(key=lambda m: -float(m["extra_minutes"]))
    # Test jobs whose log never says which tests ran: nobody can see duplicates, flaky tests or slow tests there.
    blind: list[dict[str, Any]] = [
        {"job": j.label, "minutes": round(j.dur / 60, 1)}
        for j in jobs
        if not j.units.runs and TEST_JOB.search(j.job) and j.dur >= 120
    ]
    unique_tests = sum(weight.values())
    tests_run = sum(j.units.tests for j in testers)
    kinds = Counter(j.units.kind for j in testers if j.units.kind)
    return {
        "repo": repo,
        "sha": choice["sha"][:7],
        "pr": choice["pr"],
        "workflows": len({r["name"] for r in choice["runs"]}),
        "runs": len(choice["runs"]),
        "events": sorted({str(r["event"]) for r in choice["runs"]}),
        "jobs": len(jobs),
        "jobs_not_read": skipped_jobs,
        "test_jobs": len(testers),
        "kind": kinds.most_common(1)[0][0] if kinds else None,
        "unique_units": len(seen_in),
        "unique_tests": unique_tests,
        "tests_run": tests_run,
        "factor": round(tests_run / unique_tests, 2) if unique_tests else None,
        "extra": dict(extra),
        "repeated_examples": repeated_examples,
        "always_skipped": len(always_skipped),
        "always_skipped_examples": always_skipped[:5],
        "test_minutes": round(sum(j.dur for j in testers) / 60, 1),
        "job_minutes": round(sum(j.dur for j in jobs) / 60, 1),
        "redundant_minutes": round(sum(float(r["minutes"]) for r in redundant.values()), 1),
        "redundant": list(redundant.values()),
        "matrix": matrix,
        "matrix_extra_minutes": round(sum(float(m["extra_minutes"]) for m in matrix), 1),
        "blind": blind,
        "blind_minutes": round(sum(float(b["minutes"]) for b in blind), 1),
        "pairs": pairs[:10],
        "jobs_detail": [
            {
                "workflow": j.workflow,
                "job": j.job,
                "event": j.event,
                "minutes": round(j.dur / 60, 1),
                "result": j.result,
                "scan": j.scan,
                **(
                    j.units.summary()
                    or {"kind": None, "units": 0, "tests": 0, "repeated": 0, "skipped": 0, "test_span_min": 0}
                ),
            }
            for j in sorted(jobs, key=lambda j: (j.workflow, j.job))
        ],
    }


def totals(jobs: Sequence[dict[str, Any]], missing: int, calls: int) -> dict[str, Any]:
    failure_classes = Counter(j["failure"]["class"] for j in jobs if j.get("failure"))
    seconds = Counter[str]()
    for job in jobs:
        if job["result"] == "success":
            seconds.update(job["act"])
    busy = sum(seconds.values()) or 1
    hits = sum(j["cache"]["hit"] for j in jobs)
    misses = sum(j["cache"]["miss"] for j in jobs)
    return {
        "jobs": len(jobs),
        "repos": len({j["repo"] for j in jobs}),
        "failed": sum(1 for j in jobs if j["result"] == "failure"),
        "succeeded": sum(1 for j in jobs if j["result"] == "success"),
        "lines": sum(j["lines"] for j in jobs),
        "megabytes": round(sum(j["bytes"] for j in jobs) / 1e6, 1),
        "job_minutes": round(sum(j["dur"] for j in jobs) / 60, 1),
        "quiet_share": round(sum(j["quiet"] for j in jobs) / max(1.0, sum(j["dur"] for j in jobs)), 3),
        "errors": sum(j["errors"] for j in jobs),
        "warnings": sum(j["warnings"] for j in jobs),
        "deprecation_warnings": sum(j["warn_kinds"].get("deprecation", 0) for j in jobs),
        "cache_hits": hits,
        "cache_misses": misses,
        "activity_share_ok_jobs": {a: round(seconds[a] / busy, 3) for a in ACTIVITIES},
        "failure_classes": dict(failure_classes.most_common()),
        "missing_logs": missing,
        "api_calls": calls,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", default="ci-observatory/data/runs.json", help="run table written by the CI collector")
    parser.add_argument("--out", default="ci-observatory/data/logs.json")
    parser.add_argument("--cache-dir", default=os.path.join(os.environ.get("XDG_CACHE_HOME", "~/.cache"), "ci-observatory"))
    parser.add_argument("--repos", nargs="*", help="only these owner/name repositories")
    parser.add_argument("--per-repo-failed", type=int, default=3, help="failed runs to read per repository")
    parser.add_argument("--per-repo-ok", type=int, default=4, help="successful runs to read per repository")
    parser.add_argument("--jobs-per-failed-run", type=int, default=2)
    parser.add_argument("--max-calls", type=int, default=1500, help="stop after this many API calls")
    parser.add_argument("--max-mb", type=float, default=1500, help="stop downloading after this many megabytes")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--offline", action="store_true", help="use only cached job lists and logs")
    parser.add_argument("--dry-run", action="store_true", help="print the runs it would read and exit")
    parser.add_argument("--audit-out", default="ci-observatory/data/tests.json", help="test redundancy audit output")
    parser.add_argument("--audit-jobs", type=int, default=18, help="most jobs to read for each audited commit")
    parser.add_argument("--skip-sample", action="store_true", help="only run the test redundancy audit")
    parser.add_argument("--skip-audit", action="store_true", help="only read the sampled jobs")
    args = parser.parse_args(argv)

    if not args.offline and not args.dry_run and (problem := unavailable()):
        print(f"error: {problem}", file=sys.stderr)
        return 2
    rows: list[dict[str, Any]] = json.loads(Path(args.runs).read_text())
    if args.repos:
        rows = [r for r in rows if r["repo"] in set(args.repos)]
    plan = pick_runs(rows, args.per_repo_failed, args.per_repo_ok)
    if args.dry_run:
        for repo, picks in sorted(plan.items()):
            for run, reason in picks:
                print(f"{repo:55} {reason:6} {run['ts']} {run['event']:13} {run['workflow']}")
        print(f"{sum(len(p) for p in plan.values())} runs in {len(plan)} repositories")
        return 0

    root = Path(args.cache_dir).expanduser()
    gh = None if args.offline else Gh("", args.max_calls, args.workers)
    incomplete = False
    if not args.skip_sample:
        incomplete |= read_sample(args, plan, gh, root)
    if not args.skip_audit:
        incomplete |= run_audit(args, sorted(plan), gh, root)
    return 1 if incomplete else 0


def read_sample(
    args: argparse.Namespace, plan: dict[str, list[tuple[dict[str, Any], str]]], gh: Gh | None, root: Path
) -> bool:
    """Download and parse the sampled jobs' logs and write the telemetry file. Returns True when something was missed."""
    incomplete = False

    def jobs_for(item: tuple[str, dict[str, Any], str]) -> list[Choice]:
        repo, run, reason = item
        try:
            jobs = list_jobs(gh, root, repo, int(run["id"]))
        except GhError as exc:
            print(f"  skip run {repo} {run['id']}: {exc}", file=sys.stderr)
            return []
        return [Choice(repo, run, job, reason) for job in pick_jobs(jobs or [], reason, args.jobs_per_failed_run)]

    work = [(repo, run, reason) for repo, picks in sorted(plan.items()) for run, reason in picks]
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        choices = [c for found in pool.map(jobs_for, work) for c in found]
    print(f"{len(work)} runs -> {len(choices)} jobs to read", file=sys.stderr)

    downloaded = 0.0
    budget_lock = Lock()
    missing = 0

    def read(choice: Choice) -> dict[str, Any] | None:
        nonlocal downloaded, missing, incomplete
        with budget_lock:
            if downloaded / 1e6 >= args.max_mb:
                incomplete = True
                return None
        try:
            raw = fetch_log(gh, root, choice.repo, int(choice.job["id"]))
        except GhError as exc:
            with budget_lock:
                missing += 1
                incomplete = True
            print(f"  no log for {choice.repo} job {choice.job['id']}: {exc}", file=sys.stderr)
            return None
        if raw is None:
            with budget_lock:
                missing += 1
            return None
        with budget_lock:
            downloaded += len(raw)
        meta = {
            "repo": choice.repo,
            "run": choice.run,
            "job": choice.job,
            "steps": choice.job["steps"],
            "reason": choice.reason,
        }
        parsed = analyse(raw, meta)
        if parsed:
            print(
                f"  {choice.repo:50} {parsed['result']:8} {parsed['lines']:>7} lines  {choice.job['name']}", file=sys.stderr
            )
        return parsed

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = [j for j in pool.map(read, choices) if j]
    jobs.sort(key=lambda j: (j["repo"], j["start"] or ""))

    calls = gh.calls if gh else 0
    out: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": "GitHub Actions job logs through the gh CLI, GET requests only. Excerpts are cleaned of names.",
        "params": {
            "per_repo_failed": args.per_repo_failed,
            "per_repo_ok": args.per_repo_ok,
            "jobs_per_failed_run": args.jobs_per_failed_run,
            "gap_s": GAP_S,
        },
        "activities": list(ACTIVITIES),
        "band_codes": BAND_CODE,
        "totals": totals(jobs, missing, calls),
        "jobs": jobs,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False))
    t = out["totals"]
    print(
        f"wrote {args.out}: {t['jobs']} jobs ({t['failed']} failed) from {t['repos']} repos, {t['lines']:,} lines,"
        f" {t['megabytes']} MB, {calls} API calls, {missing} missing",
        file=sys.stderr,
    )
    return incomplete


def run_audit(args: argparse.Namespace, repos: Sequence[str], gh: Gh | None, root: Path) -> bool:
    """Audit one recent pull request commit per repository for tests that ran more than once. Returns True when a
    repository could not be audited."""
    incomplete = False

    def one(repo: str) -> dict[str, Any] | None:
        nonlocal incomplete
        try:
            found = audit_repo(gh, root, repo, args.audit_jobs)
        except GhError as exc:
            incomplete = True
            print(f"  audit skipped {repo}: {exc}", file=sys.stderr)
            return None
        if found:
            print(
                f"  audit {repo:50} {found['test_jobs']:>2}/{found['jobs']} jobs ran tests, x{found['factor']}",
                file=sys.stderr,
            )
        return found

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        audits = [a for a in pool.map(one, repos) if a]
    extra = Counter[str]()
    for audit in audits:
        extra.update(audit["extra"])
    out: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": "GitHub Actions job logs through the gh CLI, GET requests only.",
        "method": (
            "For each repository, the newest pull request commit whose checks passed on at least two workflows. Every"
            " job of every run on that commit (all events) is read, longest first, and the tests it ran are taken from"
            " its log: pytest and Go test cases, Maven test classes. A test that ran in more than one job is counted"
            " as a repeat, attributed to matrix variants, the same workflow fired by two events, another workflow, or"
            " the same job."
        ),
        "summary": {
            "repos": len(audits),
            "tests_run": sum(a["tests_run"] for a in audits),
            "unique_tests": sum(a["unique_tests"] for a in audits),
            "extra": dict(extra),
            "redundant_minutes": round(sum(a["redundant_minutes"] for a in audits), 1),
            "matrix_extra_minutes": round(sum(a["matrix_extra_minutes"] for a in audits), 1),
            "blind_minutes": round(sum(a["blind_minutes"] for a in audits), 1),
            "test_minutes": round(sum(a["test_minutes"] for a in audits), 1),
            "always_skipped": sum(a["always_skipped"] for a in audits),
        },
        "repos": sorted(audits, key=lambda a: str(a["repo"])),
    }
    Path(args.audit_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.audit_out).write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False))
    s = out["summary"]
    print(
        f"wrote {args.audit_out}: {s['repos']} commits, {s['tests_run']:,} test runs of {s['unique_tests']:,} tests,"
        f" {s['redundant_minutes']} redundant job minutes",
        file=sys.stderr,
    )
    return incomplete


if __name__ == "__main__":
    sys.exit(main())
