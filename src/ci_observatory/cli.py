"""ci-observatory: one command that maps CI/CD speed, failures, repeated tests, DORA metrics and AI use across GitHub
repositories, public or private, and writes a single self-contained HTML report.

    ci-observatory                                  # the repository of the current directory
    ci-observatory --org my-org                     # the busiest repositories of an organisation
    ci-observatory --repo my-org/api --repo my-org/web --days 30 --open

Everything is read with GET requests through the GitHub CLI, so this tool never sees your token (without `gh`, it uses
GH_TOKEN directly). Private repositories work when your login can read them: `gh auth login` (or GH_TOKEN) with the
repo scope, authorised for SSO if your organisation uses it. Steps:

  1. actions   runs, jobs, steps, workflow hygiene, pull requests, releases
  2. logs      a sample of job logs, decoded, then a test redundancy audit of one PR commit per repository
  3. signals   AI involvement (agents, models, AI review, AI in CI) and DORA-style delivery numbers
  4. report    ci-observatory/index.html with everything inlined
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import webbrowser
from collections.abc import Sequence
from pathlib import Path

from ci_observatory import __version__, actions, logs, report, signals
from ci_observatory.gh import Gh, GhError, RateLimited, transport

REPO = re.compile(r"^[A-Za-z0-9_.][A-Za-z0-9_.-]*/[A-Za-z0-9_.][A-Za-z0-9_.-]*$")


def say(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def gh_problem() -> str | None:
    """Why GitHub cannot be read, or None when it can."""
    if transport() == "http":
        return None  # a token from the environment, used directly: gh is not needed
    if shutil.which("gh") is None:
        return (
            "the GitHub CLI `gh` is not installed. Install it from https://cli.github.com and run `gh auth login`,"
            " or set GH_TOKEN to a token that can read your repositories."
        )
    if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_ENTERPRISE_TOKEN"):
        return None
    proc = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return "gh is not logged in. Run `gh auth login`, or set GH_TOKEN to a token that can read your repositories."
    return None


def current_repo() -> str | None:
    """owner/name of the `origin` remote of the current directory, if it is a GitHub repository."""
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    match = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", url)
    return f"{match.group(1)}/{match.group(2)}" if match else None


def can_read(repo: str) -> tuple[bool, bool]:
    """(readable, private) for one repository. A rate limit is not "unreadable": it propagates."""
    try:
        found = Gh(max_calls=5).json(f"repos/{repo}")
    except RateLimited:
        raise
    except GhError:
        return False, False
    return found is not None, bool(found and found.get("private"))


def shareable(argv: Sequence[str]) -> str:
    """The command line as the report shows it: without --out and --open, which only make sense on this machine (and
    a path can name a local user)."""
    kept: list[str] = []
    skip = False
    for arg in argv:
        if skip:
            skip = False
        elif arg == "--out":
            skip = True
        elif not (arg.startswith("--out=") or arg == "--open"):
            kept.append(arg)
    return shlex.join(["ci-observatory", *kept])


def step(name: str, code: int) -> bool:
    if code == 0:
        return True
    if code == 1:
        say(f"  ! {name}: finished with gaps (a limit was reached or some data was missing); the report notes it")
        return True
    say(f"  ✕ {name} failed (exit {code})")
    return False


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ci-observatory", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--org", action="append", default=[], metavar="ORG", help="organisation or user to scan (repeatable)"
    )
    parser.add_argument("--repo", action="append", default=[], metavar="OWNER/NAME", help="repository to scan (repeatable)")
    parser.add_argument("--top", type=int, default=12, help="with --org: how many of the busiest repositories (default 12)")
    parser.add_argument("--min-runs", type=int, default=10, help="with --org: skip repositories with fewer runs")
    parser.add_argument(
        "--visibility", choices=("all", "public", "private"), default="all", help="with --org: which repositories"
    )
    parser.add_argument(
        "--all-active",
        action="store_true",
        help="with --org: also forks and repositories nobody pushed to, if they ran CI in the window",
    )
    parser.add_argument("--days", type=int, default=30, help="look back this many days (default 30)")
    parser.add_argument("--out", type=Path, default=Path("ci-observatory"), help="output folder (default ./ci-observatory)")
    parser.add_argument("--title", help="report heading (default: CI Observatory)")
    parser.add_argument("--quick", action="store_true", help="smaller samples: fewer API calls and log downloads")
    parser.add_argument("--no-logs", action="store_true", help="skip job logs and the test audit")
    parser.add_argument("--no-tests", action="store_true", help="skip the test redundancy audit")
    parser.add_argument("--no-ai", action="store_true", help="skip AI and DORA signals")
    parser.add_argument("--max-calls", type=int, default=4000, help="API call budget per step (default 4000)")
    parser.add_argument("--open", action="store_true", help="open the report in your browser when it is ready")
    parser.add_argument("--version", action="version", version=f"ci-observatory {__version__}")
    args = parser.parse_args(argv)

    problem = gh_problem()
    if problem:
        say(f"error: {problem}")
        return 2
    repos = list(dict.fromkeys(args.repo))
    if not repos and not args.org:
        here = current_repo()
        if not here:
            say("error: say what to scan: --org ORG, --repo OWNER/NAME, or run inside a clone of a GitHub repository.")
            return 2
        say(f"No --org or --repo given: scanning this directory's repository, {here}.")
        repos = [here]
    bad = [r for r in repos if not REPO.match(r)]
    if bad:
        say(f"error: not OWNER/NAME: {', '.join(bad)}")
        return 2
    for repo in repos:
        try:
            readable, private = can_read(repo)
        except RateLimited:
            say("error: GitHub's API rate limit is used up for this token. Wait for the hourly reset, then rerun.")
            return 1
        if not readable:
            say(
                f"error: cannot read {repo}. For a private repository, the token needs the repo scope "
                "(`gh auth refresh -s repo,read:org`) and, if the organisation uses SAML SSO, authorisation for it."
            )
            return 2
        if private:
            say(f"  {repo} is private: its data stays in {args.out}/ on this machine.")

    data = args.out / "data"
    data.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    budget = ["--max-calls", str(args.max_calls)]

    say("\n[1/4] Workflow runs, jobs and hygiene")
    first = ["--days", str(args.days), "--out", str(data / "metrics.json"), "--dash-dir", str(data), *budget]
    if repos:
        first += ["--repos", *repos]
    else:
        first += [
            "--orgs",
            *args.org,
            "--top",
            str(args.top),
            "--min-runs",
            str(args.min_runs),
            "--visibility",
            args.visibility,
        ]
        if args.all_active:
            first.append("--all-active")
    if args.quick:
        first += ["--jobs-per-workflow", "3", "--max-job-calls", "18", "--max-runs", "500"]
    if not step("actions", actions.main(first)):
        return 2
    scanned = [row["repo"] for row in json.loads((data / "repos.json").read_text())]
    if not scanned:
        say("No repository had workflow runs in the window. Try a longer --days or other repositories.")
        return 1

    if not args.no_logs:
        say("\n[2/4] Job logs and the test redundancy audit")
        second = [
            "--runs",
            str(data / "runs.json"),
            "--out",
            str(data / "logs.json"),
            "--audit-out",
            str(data / "tests.json"),
            *budget,
        ]
        if args.quick:
            second += ["--per-repo-failed", "2", "--per-repo-ok", "2", "--audit-jobs", "10"]
        if args.no_tests:
            second.append("--skip-audit")
        step("logs", logs.main(second))
    if not args.no_ai:
        say("\n[3/4] AI involvement and DORA signals")
        third = [
            "--repos",
            *scanned,
            "--runs",
            str(data / "runs.json"),
            "--days",
            str(args.days),
            "--out",
            str(data / "signals.json"),
            *budget,
        ]
        step("signals", signals.main(third))

    say("\n[4/4] Report")
    who = ", ".join(args.org) or (repos[0] if len(repos) == 1 else f"{len(scanned)} repositories")
    heading = args.title or "CI Observatory"
    paths = {key: data / name for key, name in report.DATA_FILES.items()}
    out = report.build(
        paths,
        args.out / "index.html",
        heading=heading,
        orgs=who,
        command=shareable(argv if argv is not None else sys.argv[1:]),
        note=(
            "Contains workflow names, PR titles of AI-assisted changes and cleaned log excerpts:"
            " share it like the repositories themselves."
        ),
    )
    say(f"\nDone in {time.monotonic() - started:.0f} s. Report: {out.resolve()}")
    say(f"Open it with: open {out}  (or any browser; it works offline except for web fonts)")
    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
