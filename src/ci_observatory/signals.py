"""AI involvement and DORA-style delivery signals for GitHub repositories, read-only.

For each repository this reads, through `gh api --method GET`:

- which agent instruction files exist (AGENTS.md, CLAUDE.md, .github/copilot-instructions.md, Cursor rules, ...);
- which workflows involve AI: Copilot code review and the Copilot coding agent (GitHub runs both on Actions), and
  workflow files that call AI tools or gate AI-written content;
- who started each workflow run: a person, an automation bot, an AI agent or a schedule (people are never named);
- pull requests opened in the window, with their commits and reviews. The AI agents and models behind a change come
  from commit trailers ("Co-authored-by: Claude", "Assisted-by: Claude Opus 5.5"), the PR description ("Generated with
  Claude Code") and AI accounts that author PRs. Each change is followed through CI failures, fix-up rounds, requested
  changes, reverts and later fixes, so agents and models can be compared on how many problems their changes caused;
- commits on the default branch, releases and runs on the default branch,

and turns them into DORA-style numbers per repository: deployment frequency, lead time for changes, change failure
rate and time to restore. Public repositories expose no production telemetry, so these are proxies and the output
says which proxy each number uses.

    python -m ci_observatory.signals --repos my-org/api my-org/web --runs ci-observatory/data/runs.json
"""

import argparse
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ci_observatory.gh import BudgetExceeded, Gh, GhError, RateLimited, unavailable

SCHEMA_VERSION = 1

# Accounts that write or review code with AI. Logins are compared in lower case.
AI_ACCOUNTS: dict[str, tuple[str, str]] = {
    "copilot": ("GitHub Copilot", "author"),
    "copilot-swe-agent[bot]": ("Copilot coding agent", "author"),
    "copilot-pull-request-reviewer[bot]": ("Copilot code review", "reviewer"),
    "devin-ai-integration[bot]": ("Devin", "author"),
    "claude[bot]": ("Claude Code", "author"),
    "google-labs-jules[bot]": ("Jules", "author"),
    "chatgpt-codex-connector[bot]": ("Codex", "author"),
    "openai-codex[bot]": ("Codex", "author"),
    "cursor[bot]": ("Cursor", "author"),
    "sweep-ai[bot]": ("Sweep", "author"),
    "coderabbitai[bot]": ("CodeRabbit", "reviewer"),
    "gemini-code-assist[bot]": ("Gemini Code Assist", "reviewer"),
    "sourcery-ai[bot]": ("Sourcery", "reviewer"),
    "qodo-merge-pro[bot]": ("Qodo", "reviewer"),
    "qodo-code-review[bot]": ("Qodo", "reviewer"),
    "greptile-apps[bot]": ("Greptile", "reviewer"),
    "ellipsis-dev[bot]": ("Ellipsis", "reviewer"),
    "korbit-ai[bot]": ("Korbit", "reviewer"),
    "cubic-dev-ai[bot]": ("cubic", "reviewer"),
    "bito-code-review[bot]": ("Bito", "reviewer"),
}
AUTOMATION_ACCOUNTS = {
    "dependabot[bot]": "Dependabot",
    "renovate[bot]": "Renovate",
    "github-actions[bot]": "GitHub Actions",
    "pre-commit-ci[bot]": "pre-commit.ci",
    "mergify[bot]": "Mergify",
    "release-please[bot]": "release-please",
    "codecov[bot]": "Codecov",
}

# Who wrote a change with AI, from commit trailers and PR descriptions. A Co-authored-by trailer also names people, so
# it only counts when it is unmistakably an AI (a vendor address or an agent's own name). Assisted-by and similar
# trailers are always about AI; when one names only a model, the agent is recorded as "Agent not named".
TRAILER = re.compile(
    r"^[ \t]*(co-authored-by|assisted-by|generated-by|ai-assisted-by|made-with|created-with|written-with)[ \t]*:"
    r"[ \t]*(.+?)[ \t]*$",
    re.I | re.M,
)
COAUTHOR_AGENTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Claude Code", re.compile(r"noreply@anthropic\.com|claude[ .-]?code", re.I)),
    ("Copilot Autofix", re.compile(r"copilot autofix", re.I)),
    ("Copilot coding agent", re.compile(r"copilot-swe-agent", re.I)),
    ("GitHub Copilot", re.compile(r"\bcopilot\b", re.I)),
    ("Cursor", re.compile(r"@cursor\.com|cursor[ -]?agent|^cursor\s*<", re.I)),
    ("Codex", re.compile(r"\bcodex\b|chatgpt|@openai\.com", re.I)),
    ("Gemini", re.compile(r"gemini[ -](?:code|cli)|google-labs-jules|\bjules\b", re.I)),
    ("Devin", re.compile(r"devin-ai|@devin\.ai", re.I)),
    ("Aider", re.compile(r"\baider\b", re.I)),
    ("OpenHands", re.compile(r"openhands", re.I)),
)
NAMED_AGENTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    *COAUTHOR_AGENTS,
    ("Cursor", re.compile(r"\bcursor\b|\bcomposer\b", re.I)),
    ("Devin", re.compile(r"\bdevin\b", re.I)),
    ("Windsurf", re.compile(r"windsurf|codeium", re.I)),
    ("Amazon Q", re.compile(r"amazon q\b|codewhisperer|\bkiro\b", re.I)),
    ("JetBrains Junie", re.compile(r"\bjunie\b", re.I)),
    ("Cline", re.compile(r"\bcline\b|\broo ?code\b", re.I)),
)
GENERATED_WITH = re.compile(
    r"generated (?:with|by|using) \[?(claude code|claude|cursor|copilot|codex|chatgpt|gemini|devin|aider|windsurf|junie)",
    re.I,
)
GENERATED_NAMES = {
    "claude code": "Claude Code",
    "claude": "Claude Code",
    "copilot": "GitHub Copilot",
    "chatgpt": "Codex",
    "junie": "JetBrains Junie",
}
MODEL = re.compile(
    r"\b(claude)[ -]+(opus|sonnet|haiku|fable)(?:[ -]*(\d+(?:\.\d+)?))?"
    r"|\b(gpt)-?(\d+(?:\.\d+)?(?:-?(?:mini|nano|codex|turbo))?)"
    r"|\b(gemini)[ -]*(\d+(?:\.\d+)?)(?:[ -]*(pro|flash))?"
    r"|\b(grok)(?:[ -]*(\d+(?:\.\d+)?))?"
    r"|\b(composer)(?:[ -]*(\d+(?:\.\d+)?))?",
    re.I,
)

AGENT_FILES = {
    "AGENTS.md": "AGENTS.md",
    "CLAUDE.md": "CLAUDE.md",
    "GEMINI.md": "GEMINI.md",
    ".cursorrules": "Cursor rules",
    ".cursor": "Cursor rules",
    ".windsurfrules": "Windsurf rules",
    ".clinerules": "Cline rules",
    ".aider.conf.yml": "Aider config",
    ".claude": "Claude Code settings",
    ".mcp.json": "MCP servers",
    "llms.txt": "llms.txt",
    ".coderabbit.yaml": "CodeRabbit config",
    ".devcontainer": "Dev container",
}
GITHUB_FILES = {
    "copilot-instructions.md": "Copilot instructions",
    "instructions": "Copilot path instructions",
    "prompts": "Prompt files",
    "agents": "Custom agents",
    "chatmodes": "Copilot chat modes",
    "CODEOWNERS": "CODEOWNERS",
    "pull_request_template.md": "PR template",
    "PULL_REQUEST_TEMPLATE": "PR template",
}
AI_WORKFLOW_TEXT = re.compile(
    r"anthropics/claude-code(?:-base)?-action|openai/codex-action|google-github-actions/run-gemini-cli|coderabbit"
    r"|qodo|pr-agent|ANTHROPIC_API_KEY|OPENAI_API_KEY|GEMINI_API_KEY|CLAUDE_CODE_OAUTH_TOKEN|copilot"
    r"|ai-tells|AI-written|AI-generated|AI-attributed|Assisted-by",
    re.I,
)
AI_GATE_TEXT = re.compile(r"ai-tells|AI-written|AI-generated|AI-attributed|Assisted-by|vale", re.I)
MAX_RUN_SECONDS = 6 * 3600
DEPLOY = re.compile(r"deploy|publish|release|pages|\bdocs?\b|helm|chart|goreleaser", re.I)
DEPLOY_EVENTS = frozenset({"push", "release", "workflow_dispatch", "dynamic"})
CI_EVENTS = frozenset({"push"})
REVERT_SHA = re.compile(r"This reverts commit ([0-9a-f]{7,40})")
REVERT_TITLE = re.compile(r'^Revert "(.+)"', re.M)
PR_IN_MESSAGE = re.compile(r"\(#(\d+)\)\s*$|Merge pull request #(\d+)", re.M)
SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)")
FIX_WORDS = r"(?:fix|fixes|fixed|hotfix|revert|regression|follow[- ]?up|broke|broken|bug)"


def parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def hours(a: datetime, b: datetime) -> float:
    return round((b - a).total_seconds() / 3600, 2)


def median(values: Iterable[float]) -> float | None:
    data = sorted(values)
    return round(statistics.median(data), 2) if data else None


def account(user: dict[str, Any] | None) -> tuple[str, str]:
    """(kind, name) for a GitHub account. kind is human, ai, bot or unknown. People are never named."""
    if not user:
        return "unknown", "unknown"
    login = str(user.get("login") or "")
    key = login.lower()
    if key in AI_ACCOUNTS:
        return "ai", AI_ACCOUNTS[key][0]
    if key in AUTOMATION_ACCOUNTS:
        return "bot", AUTOMATION_ACCOUNTS[key]
    if user.get("type") == "Bot" or key.endswith("[bot]"):
        return "bot", login
    return "human", "human"


def model_names(text: str) -> set[str]:
    found: set[str] = set()
    for match in MODEL.finditer(text):
        g = match.groups()
        if g[0]:
            found.add(" ".join(p for p in ("Claude", g[1].title(), g[2]) if p))
        elif g[3]:
            found.add(f"GPT-{g[4]}")
        elif g[5]:
            found.add(" ".join(p for p in ("Gemini", g[6], (g[7] or "").title()) if p))
        elif g[8]:
            found.add(" ".join(p for p in ("Grok", g[9]) if p))
        elif g[10]:
            found.add(" ".join(p for p in ("Cursor Composer", g[11]) if p))
    return found


def ai_mentions(text: str) -> tuple[set[str], set[str]]:
    """AI agents and models named in a commit message or PR description."""
    agents: set[str] = set()
    models: set[str] = set()
    for kind, value in TRAILER.findall(text or ""):
        coauthor = kind.lower() == "co-authored-by"
        name = next(
            (agent for agent, pattern in (COAUTHOR_AGENTS if coauthor else NAMED_AGENTS) if pattern.search(value)), None
        )
        found = model_names(value)
        if name is None and coauthor:
            continue  # a person
        if name is None:
            name = "Agent not named"
            cleaned = re.sub(r"<[^>]*>", "", value).strip()[:40]
            if not found and cleaned:
                found.add(cleaned)
        agents.add(name)
        models |= found
    for name in GENERATED_WITH.findall(text or ""):
        agents.add(GENERATED_NAMES.get(name.lower(), name.title()))
    if len(agents) > 1:
        agents.discard("Agent not named")
    return agents, models


def week(moment: datetime) -> str:
    return (moment - timedelta(days=moment.weekday())).strftime("%Y-%m-%d")


class RepoSignals:
    def __init__(
        self, gh: Gh, repo: str, since: datetime, until: datetime, runs_index: dict[str, list[dict[str, Any]]]
    ) -> None:
        self.gh = gh
        self.repo = repo
        self.since = since
        self.until = until
        self.runs_index = runs_index
        self.default_branch = "main"
        self.notes: list[str] = []

    # ---- reading ----------------------------------------------------------------------------------------------

    def pages(
        self, path: str, params: dict[str, str | int], key: str | None, stop: Any = None, limit: int = 10
    ) -> list[Any]:
        found: list[Any] = []
        for page in range(1, limit + 1):
            data = self.gh.json(path, {**params, "per_page": 100, "page": page})
            batch = (data or {}).get(key, []) if key else (data or [])
            found.extend(batch)
            if len(batch) < 100 or (stop and stop(batch)):
                break
        return found

    def files(self) -> dict[str, Any]:
        present: dict[str, Any] = {}
        root = self.gh.json(f"repos/{self.repo}/contents/") or []
        for entry in root:
            if entry.get("name") in AGENT_FILES:
                present[AGENT_FILES[entry["name"]]] = {"path": entry["path"], "size": entry.get("size") or 0}
        github = self.gh.json(f"repos/{self.repo}/contents/.github") or []
        for entry in github if isinstance(github, list) else []:
            if entry.get("name") in GITHUB_FILES:
                present[GITHUB_FILES[entry["name"]]] = {"path": entry["path"], "size": entry.get("size") or 0}
        return present

    def workflows(self) -> list[dict[str, Any]]:
        data = self.gh.json(f"repos/{self.repo}/actions/workflows", {"per_page": 100}) or {}
        return list(data.get("workflows", []))

    def ai_workflows(self, workflows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for wf in workflows:
            path = str(wf.get("path") or "")
            kind: str | None = None
            tools: list[str] = []
            if path.startswith("dynamic/") and "copilot" in path:
                kind = "copilot-review" if "reviewer" in path else "copilot-agent"
                tools = ["Copilot code review" if kind == "copilot-review" else "Copilot coding agent"]
            elif path.startswith(".github/workflows/") and wf.get("state") == "active":
                text = self.gh.text(f"repos/{self.repo}/contents/{path}") or ""
                if path.endswith("copilot-setup-steps.yml"):
                    kind, tools = "copilot-setup", ["Copilot coding agent"]
                elif AI_WORKFLOW_TEXT.search(text):
                    names = sorted({m.group(0) for m in AI_WORKFLOW_TEXT.finditer(text)}, key=str.lower)
                    kind = (
                        "ai-gate"
                        if AI_GATE_TEXT.search(text) and not re.search(r"claude-code|codex-action|gemini-cli", text)
                        else "ai-tool"
                    )
                    tools = names[:6]
            if kind:
                found.append({"id": wf["id"], "name": wf.get("name"), "path": path, "kind": kind, "tools": tools})
        for wf in found:
            runs = self.pages(
                f"repos/{self.repo}/actions/workflows/{wf['id']}/runs",
                {"created": f">={self.since:%Y-%m-%d}", "status": "completed"},
                "workflow_runs",
                limit=5,
            )
            seconds = [
                min(MAX_RUN_SECONDS, max(0.0, (t2 - t1).total_seconds()))
                for r in runs
                if (t1 := parse_time(r.get("run_started_at"))) and (t2 := parse_time(r.get("updated_at")))
            ]
            results = Counter(str(r.get("conclusion")) for r in runs)
            wf.update(
                {
                    "runs": len(runs),
                    "success": results.get("success", 0),
                    "failure": results.get("failure", 0),
                    "minutes": round(sum(seconds) / 60, 1),
                    "median_s": round(statistics.median(seconds)) if seconds else None,
                    "events": dict(Counter(str(r.get("event")) for r in runs)),
                    "weeks": dict(Counter(week(when) for r in runs if (when := parse_time(r.get("created_at"))))),
                }
            )
            wf.pop("id")
        return found

    def runs(self) -> list[dict[str, Any]]:
        """Every completed run in the window, slimmed to what the signals need. Same request as the CI collector, so
        a warm `gh` cache answers it."""
        keep: list[dict[str, Any]] = []
        for run in self.pages(
            f"repos/{self.repo}/actions/runs",
            {"status": "completed", "created": f">={self.since:%Y-%m-%d}"},
            "workflow_runs",
        ):
            actor_kind, actor_name = account(run.get("triggering_actor") or run.get("actor"))
            started, ended = parse_time(run.get("run_started_at")), parse_time(run.get("updated_at"))
            seconds = (ended - started).total_seconds() if started and ended else 0.0
            if run.get("conclusion") in ("action_required", "skipped"):
                seconds = 0.0  # never executed
            # A re-run moves updated_at days later; no hosted job runs longer than 6 hours, so cap there.
            seconds = min(max(0.0, seconds), MAX_RUN_SECONDS)
            keep.append(
                {
                    "id": run["id"],
                    "name": run.get("name"),
                    "path": run.get("path"),
                    "event": run.get("event"),
                    "main": run.get("head_branch") == self.default_branch,
                    "sha": str(run.get("head_sha") or "")[:7],
                    "who": "schedule" if run.get("event") == "schedule" else actor_kind,
                    "agent": actor_name if actor_kind in ("ai", "bot") else None,
                    "result": run.get("conclusion"),
                    "created": parse_time(run.get("created_at")),
                    "ended": ended,
                    "seconds": seconds,
                }
            )
        return keep

    def pulls(self) -> list[dict[str, Any]]:
        since = self.since

        def older(batch: list[dict[str, Any]]) -> bool:
            return bool(batch) and (parse_time(batch[-1].get("created_at")) or since) < since

        found = self.pages(
            f"repos/{self.repo}/pulls", {"state": "all", "sort": "created", "direction": "desc"}, None, stop=older, limit=6
        )
        return [p for p in found if (parse_time(p.get("created_at")) or since) >= since]

    def commits(self) -> list[dict[str, Any]]:
        return self.pages(
            f"repos/{self.repo}/commits",
            {"sha": self.default_branch, "since": f"{self.since:%Y-%m-%dT%H:%M:%SZ}"},
            None,
            limit=6,
        )

    def releases(self) -> list[dict[str, Any]]:
        data = self.gh.json(f"repos/{self.repo}/releases", {"per_page": 100}) or []
        keep = []
        for r in data:
            when = parse_time(r.get("published_at"))
            if when and not r.get("draft") and self.since <= when <= self.until:
                keep.append({"tag": r.get("tag_name"), "t": when, "pre": bool(r.get("prerelease"))})
        return sorted(keep, key=lambda r: r["t"])

    # ---- pull requests ----------------------------------------------------------------------------------------

    def enrich(self, pr: dict[str, Any]) -> dict[str, Any]:
        number = int(pr["number"])
        kind, name = account(pr.get("user"))
        created = parse_time(pr.get("created_at"))
        merged = parse_time(pr.get("merged_at"))
        closed = parse_time(pr.get("closed_at"))
        agents, models = ai_mentions(pr.get("body") or "")
        if kind == "ai":
            # A PR opened by the Copilot account is the Copilot coding agent's work.
            agents.add("Copilot coding agent" if name == "GitHub Copilot" else name)
        row: dict[str, Any] = {
            "n": number,
            "created": created,
            "merged": merged,
            "closed": closed,
            "author": kind,
            "author_name": name if kind != "human" else None,
            "agents": agents,
            "models": models,
            "commits": None,
            "shas": [],
            "reviews": {"human": 0, "ai": Counter[str](), "changes_requested": 0, "approved": 0},
            "title": pr.get("title") or "",
            "body": (pr.get("body") or "")[:4000],
            "merge_sha": str(pr.get("merge_commit_sha") or "")[:7],
        }
        if kind == "bot":
            return row
        commits = self.gh.json(f"repos/{self.repo}/pulls/{number}/commits", {"per_page": 100}) or []
        row["commits"] = len(commits)
        row["shas"] = [str(c.get("sha") or "")[:7] for c in commits]
        for commit in commits:
            more_agents, more_models = ai_mentions(str((commit.get("commit") or {}).get("message") or ""))
            row["agents"] |= more_agents
            row["models"] |= more_models
            author_kind, author_name = account(commit.get("author"))
            if author_kind == "ai":
                row["agents"].add(author_name)
        reviews = self.gh.json(f"repos/{self.repo}/pulls/{number}/reviews", {"per_page": 100}) or []
        for review in reviews:
            who, reviewer = account(review.get("user"))
            if who == "ai":
                row["reviews"]["ai"][reviewer] += 1
            elif who == "human":
                row["reviews"]["human"] += 1
                if review.get("state") == "CHANGES_REQUESTED":
                    row["reviews"]["changes_requested"] += 1
                if review.get("state") == "APPROVED":
                    row["reviews"]["approved"] += 1
        return row

    def ci_outcome(self, shas: Sequence[str]) -> dict[str, Any]:
        """How the PR's commits fared in CI, from the run table: rounds of CI, failed runs and whether the first
        round failed."""
        rounds: list[list[dict[str, Any]]] = []
        for sha in shas:
            decided = [r for r in self.runs_index.get(f"{self.repo}@{sha}", []) if r.get("result") in ("success", "failure")]
            if decided:
                rounds.append(decided)
        failed = sum(1 for group in rounds for r in group if r["result"] == "failure")
        return {
            "rounds": len(rounds),
            "runs": sum(len(g) for g in rounds),
            "failed": failed,
            "first_failed": any(r["result"] == "failure" for r in rounds[0]) if rounds else None,
            "ever_failed": failed > 0 if rounds else None,
        }

    # ---- everything for one repository ------------------------------------------------------------------------

    def collect(self) -> dict[str, Any]:
        info = self.gh.json(f"repos/{self.repo}") or {}
        self.default_branch = str(info.get("default_branch") or "main")
        files = self.files()
        workflows = self.workflows()
        ai_workflows = self.ai_workflows(workflows)
        runs = self.runs()
        raw_pulls = self.pulls()
        with ThreadPoolExecutor(max_workers=3) as pool:
            prs = list(pool.map(self.enrich, raw_pulls))
        commits = self.commits()
        releases = self.releases()
        return self.summarise(info, files, ai_workflows, runs, prs, commits, releases)

    def summarise(
        self,
        info: dict[str, Any],
        files: dict[str, Any],
        ai_workflows: list[dict[str, Any]],
        runs: list[dict[str, Any]],
        prs: list[dict[str, Any]],
        commits: list[dict[str, Any]],
        releases: list[dict[str, Any]],
    ) -> dict[str, Any]:
        # Commits on the default branch: AI trailers, reverts and which PR each came from.
        reverted_shas: set[str] = set()
        reverted_titles: set[str] = set()
        revert_times: list[datetime] = []
        commit_agents: Counter[str] = Counter()
        commit_models: Counter[str] = Counter()
        ai_commits = 0
        commit_weeks: Counter[str] = Counter()
        ai_commit_weeks: Counter[str] = Counter()
        for commit in commits:
            message = str((commit.get("commit") or {}).get("message") or "")
            when = parse_time(((commit.get("commit") or {}).get("committer") or {}).get("date"))
            agents, models = ai_mentions(message)
            author_kind, author_name = account(commit.get("author"))
            if author_kind == "ai":
                agents.add(author_name)
            if when:
                commit_weeks[week(when)] += 1
            if agents:
                ai_commits += 1
                commit_agents.update(agents)
                commit_models.update(models)
                if when:
                    ai_commit_weeks[week(when)] += 1
            for sha in REVERT_SHA.findall(message):
                reverted_shas.add(sha[:7])
                if when:
                    revert_times.append(when)
            for title in REVERT_TITLE.findall(message):
                reverted_titles.add(title.strip())

        # Pull requests: outcome of each change, then later PRs that fixed or reverted it.
        by_number = {p["n"]: p for p in prs}
        for pr in prs:
            pr["ci"] = self.ci_outcome(pr["shas"]) if pr["shas"] else None
            pr["reverted"] = bool(
                pr["merged"]
                and (
                    pr["merge_sha"] in reverted_shas
                    or any(s in reverted_shas for s in pr["shas"])
                    or pr["title"] in reverted_titles
                )
            )
            pr["fixups"] = 0
        for later in prs:
            text = f"{later['title']}\n{later['body']}"
            for number in {int(n) for n in re.findall(rf"{FIX_WORDS}[^\n]{{0,120}}?#(\d+)\b", text, re.I)}:
                target = by_number.get(number)
                if (
                    target
                    and target is not later
                    and target["merged"]
                    and later["created"]
                    and later["created"] > target["merged"]
                ):
                    target["fixups"] += 1

        # Who starts CI, and how much of CI involves AI: AI running in CI, CI started by AI, CI checking AI changes.
        ai_paths = {w["path"] for w in ai_workflows}
        ai_change_shas = {s for p in prs if p["agents"] for s in p["shas"]}
        who_runs: Counter[str] = Counter()
        who_minutes: Counter[str] = Counter()
        layer_runs: Counter[str] = Counter()
        layer_minutes: Counter[str] = Counter()
        ai_weeks: Counter[str] = Counter()
        run_weeks: Counter[str] = Counter()
        total_minutes = 0.0
        for run in runs:
            minutes = run["seconds"] / 60
            total_minutes += minutes
            who_runs[run["who"]] += 1
            who_minutes[run["who"]] += minutes
            layers = []
            if run["path"] in ai_paths:
                layers.append("ai_in_ci")
            if run["who"] == "ai":
                layers.append("started_by_ai")
            if run["sha"] in ai_change_shas and run["event"] in ("pull_request", "pull_request_target"):
                layers.append("checks_ai_change")
            for layer in layers:
                layer_runs[layer] += 1
                layer_minutes[layer] += minutes
            if run["created"]:
                run_weeks[week(run["created"])] += 1
            if layers:
                layer_runs["any"] += 1
                layer_minutes["any"] += minutes
                if run["created"]:
                    ai_weeks[week(run["created"])] += 1

        dora = self.dora(runs, prs, releases, revert_times)
        return {
            "repo": self.repo,
            "private": bool(info.get("private")),
            "default_branch": self.default_branch,
            "files": files,
            "ai_workflows": ai_workflows,
            "ci": {
                "runs": len(runs),
                "minutes": round(total_minutes, 1),
                "who_runs": dict(who_runs),
                "who_minutes": {k: round(v, 1) for k, v in who_minutes.items()},
                "bots": dict(Counter(r["agent"] for r in runs if r["who"] == "bot")),
                "ai_agents": dict(Counter(r["agent"] for r in runs if r["who"] == "ai")),
                "ai_runs": dict(layer_runs),
                "ai_minutes": {k: round(v, 1) for k, v in layer_minutes.items()},
                "weeks": dict(run_weeks),
                "ai_weeks": dict(ai_weeks),
            },
            "commits": {
                "total": len(commits),
                "ai": ai_commits,
                "agents": dict(commit_agents),
                "models": dict(commit_models),
                "weeks": dict(commit_weeks),
                "ai_weeks": dict(ai_commit_weeks),
                "reverts": len(revert_times),
            },
            "prs": [self.public_pr(p) for p in prs],
            "releases": [{"tag": r["tag"], "t": r["t"].isoformat(), "pre": r["pre"]} for r in releases],
            "dora": dora,
            "notes": self.notes,
        }

    @staticmethod
    def public_pr(pr: dict[str, Any]) -> dict[str, Any]:
        """What leaves the collector for one PR: no people, and a title only for AI-involved changes."""
        reviews = pr["reviews"]
        return {
            "n": pr["n"],
            "created": pr["created"].isoformat() if pr["created"] else None,
            "merged": pr["merged"].isoformat() if pr["merged"] else None,
            "closed": pr["closed"].isoformat() if pr["closed"] else None,
            "author": pr["author"],
            "author_name": pr["author_name"],
            "agents": sorted(pr["agents"]),
            "models": sorted(pr["models"]),
            "commits": pr["commits"],
            "ci": pr["ci"],
            "reviews": {
                "human": reviews["human"],
                "ai": dict(reviews["ai"]),
                "changes_requested": reviews["changes_requested"],
                "approved": reviews["approved"],
            },
            "reverted": pr["reverted"],
            "fixups": pr["fixups"],
            "lead_h": hours(pr["created"], pr["merged"]) if pr["created"] and pr["merged"] else None,
            "title": pr["title"][:140] if pr["agents"] or pr["reviews"]["ai"] else None,
        }

    def dora(
        self,
        runs: list[dict[str, Any]],
        prs: list[dict[str, Any]],
        releases: list[dict[str, Any]],
        revert_times: list[datetime],
    ) -> dict[str, Any]:
        window_days = max(1.0, (self.until - self.since).total_seconds() / 86400)
        deploy_runs = [
            r
            for r in runs
            if r["event"] in DEPLOY_EVENTS
            and DEPLOY.search(f"{r['name']} {r['path']}")
            and r["result"] in ("success", "failure")
            and (r["main"] or r["event"] in ("release", "dynamic"))
        ]
        stable = [r for r in releases if not r["pre"]]
        if len(stable) >= 2 or (stable and len(deploy_runs) < 2):
            source = "releases"
            deployments = [r["t"] for r in stable]
            failed = 0
            for i, rel in enumerate(stable):
                version = SEMVER.search(str(rel["tag"]))
                hotfix = False
                for later in stable[i + 1 :]:
                    if later["t"] - rel["t"] > timedelta(hours=72):
                        break
                    other = SEMVER.search(str(later["tag"]))
                    if (
                        version
                        and other
                        and other.group(1, 2) == version.group(1, 2)
                        and int(other.group(3)) > int(version.group(3))
                    ):
                        hotfix = True
                reverted = any(rel["t"] < t <= rel["t"] + timedelta(days=7) for t in revert_times)
                failed += hotfix or reverted
            attempts = len(stable)
            cfr_definition = "share of releases followed within 72 h by a patch release, or within 7 days by a revert"
        elif deploy_runs:
            source = "deploy workflow runs"
            deployments = sorted(r["ended"] for r in deploy_runs if r["result"] == "success" and r["ended"])
            attempts = len(deploy_runs)
            failed = sum(1 for r in deploy_runs if r["result"] == "failure")
            cfr_definition = "share of deploy or publish runs on the default branch that failed"
        else:
            source = None
            deployments, attempts, failed = [], 0, 0
            cfr_definition = "no releases or deploy runs in the window"

        lead: list[float] = []
        wait: list[float] = []
        pending = 0
        for pr in prs:
            if not pr["merged"] or pr["author"] == "bot" or not pr["created"]:
                continue
            after = next((d for d in deployments if d >= pr["merged"]), None)
            if after is None:
                pending += 1
                continue
            lead.append(hours(pr["created"], after))
            wait.append(hours(pr["merged"], after))

        # Time to restore: on the default branch, from a workflow's first failure to its next success.
        restores: list[float] = []
        open_incidents = 0
        red: list[tuple[datetime, datetime]] = []
        by_workflow: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for run in runs:
            if run["main"] and run["event"] in CI_EVENTS and run["result"] in ("success", "failure") and run["ended"]:
                by_workflow[str(run["path"] or run["name"])].append(run)
        for group in by_workflow.values():
            group.sort(key=lambda r: r["created"] or r["ended"])
            broken_at: datetime | None = None
            for run in group:
                if run["result"] == "failure" and broken_at is None:
                    broken_at = run["ended"]
                elif run["result"] == "success" and broken_at is not None:
                    restores.append(hours(broken_at, run["ended"]))
                    red.append((broken_at, run["ended"]))
                    broken_at = None
            if broken_at is not None:
                open_incidents += 1
                red.append((broken_at, self.until))
        red.sort()
        red_hours = 0.0
        current: tuple[datetime, datetime] | None = None
        for start, end in red:
            if current and start <= current[1]:
                current = (current[0], max(current[1], end))
            else:
                if current:
                    red_hours += (current[1] - current[0]).total_seconds() / 3600
                current = (start, end)
        if current:
            red_hours += (current[1] - current[0]).total_seconds() / 3600
        main_runs = sum(len(g) for g in by_workflow.values())
        main_failed = sum(1 for g in by_workflow.values() for r in g if r["result"] == "failure")
        return {
            "source": source,
            "deployments": len(deployments),
            "deploys_per_week": round(len(deployments) / (window_days / 7), 2),
            "deploy_times": [d.isoformat() for d in deployments],
            "lead_time_h": median(lead),
            "lead_time_p90_h": round(sorted(lead)[int(0.9 * (len(lead) - 1))], 1) if lead else None,
            "merge_to_deploy_h": median(wait),
            "lead_samples": len(lead),
            "undeployed_merges": pending,
            "cfr": round(failed / attempts, 3) if attempts else None,
            "cfr_failed": failed,
            "cfr_attempts": attempts,
            "cfr_definition": cfr_definition,
            "restore_h": median(restores),
            "restore_samples": len(restores),
            "open_incidents": open_incidents,
            "main_runs": main_runs,
            "main_failure_rate": round(main_failed / main_runs, 3) if main_runs else None,
            "main_red_share": round(red_hours / (window_days * 24), 3),
        }


def compare_agents(repos: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Outcomes of changes grouped by the AI agent or model behind them, next to changes that name no AI. The groups
    overlap (one PR can name an agent and a model) and are small, so the page shows sample sizes with every number."""
    groups: dict[tuple[str, str], list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for repo in repos:
        for pr in repo["prs"]:
            if pr["author"] == "bot":
                continue
            if not pr["agents"]:
                groups[("baseline", "No AI named")].append((repo["repo"], pr))
            for agent in pr["agents"]:
                groups[("agent", agent)].append((repo["repo"], pr))
            for model in pr["models"]:
                groups[("model", model)].append((repo["repo"], pr))
            for reviewer in pr["reviews"]["ai"]:
                groups[("reviewer", reviewer)].append((repo["repo"], pr))
            if not pr["reviews"]["ai"]:
                groups[("reviewer", "No AI review")].append((repo["repo"], pr))
    rows: list[dict[str, Any]] = []
    for (kind, name), members in groups.items():
        prs = [p for _, p in members]
        with_ci = [p for p in prs if p["ci"] and p["ci"]["rounds"]]
        merged = [p for p in prs if p["merged"]]
        rows.append(
            {
                "kind": kind,
                "name": name,
                "prs": len(prs),
                "repos": dict(Counter(r for r, _ in members)),
                "merged": len(merged),
                "with_ci": len(with_ci),
                "first_ci_failed": sum(1 for p in with_ci if p["ci"]["first_failed"]),
                "ever_failed": sum(1 for p in with_ci if p["ci"]["ever_failed"]),
                "failed_runs": sum(p["ci"]["failed"] for p in with_ci),
                "ci_runs": sum(p["ci"]["runs"] for p in with_ci),
                "ci_rounds": sum(p["ci"]["rounds"] for p in with_ci),
                "changes_requested": sum(1 for p in prs if p["reviews"]["changes_requested"]),
                "reverted": sum(1 for p in merged if p["reverted"]),
                "fixups": sum(1 for p in merged if p["fixups"]),
                "commits": sum(p["commits"] or 0 for p in prs),
                "lead_h": median(p["lead_h"] for p in merged if p["lead_h"] is not None),
            }
        )
    order = {"agent": 0, "model": 1, "baseline": 2, "reviewer": 3}
    return sorted(rows, key=lambda r: (order[r["kind"]], -r["prs"]))


def runs_index(path: str | None) -> dict[str, list[dict[str, Any]]]:
    """Pull request runs from the CI collector's run table, keyed by repository and short commit."""
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not path or not Path(path).exists():
        return index
    for row in json.loads(Path(path).read_text()):
        if row.get("sha") and row.get("event") in ("pull_request", "pull_request_target"):
            index[f"{row['repo']}@{row['sha'][:7]}"].append({"result": row.get("result")})
    return index


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repos", nargs="+", required=True, help="owner/name repositories")
    parser.add_argument("--runs", help="run table written by the CI collector (runs.json), to follow PRs through CI")
    parser.add_argument("--days", type=int, default=45, help="look back this many days (default 45)")
    parser.add_argument("--out", default="ci-observatory/data/signals.json")
    parser.add_argument("--cache", default="6h", help="gh response cache, e.g. 6h; empty to disable")
    parser.add_argument("--max-calls", type=int, default=3000, help="stop after this many API calls")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    if problem := unavailable():
        print(f"error: {problem}", file=sys.stderr)
        return 2

    until = datetime.now(UTC).replace(microsecond=0)
    since = (until - timedelta(days=args.days)).replace(hour=0, minute=0, second=0)
    gh = Gh(args.cache, args.max_calls, args.workers)
    index = runs_index(args.runs)
    incomplete = False

    def one(repo: str) -> dict[str, Any] | None:
        nonlocal incomplete
        try:
            found = RepoSignals(gh, repo, since, until, index).collect()
        except (RateLimited, BudgetExceeded, GhError) as exc:
            incomplete = True
            print(f"  skipped {repo}: {exc}", file=sys.stderr)
            return None
        ai_prs = sum(1 for p in found["prs"] if p["agents"])
        print(f"  {repo:50} {len(found['prs']):>3} PRs ({ai_prs} name AI), {found['ci']['runs']:>5} runs", file=sys.stderr)
        return found

    with ThreadPoolExecutor(max_workers=max(1, args.workers // 2)) as pool:
        repos = [r for r in pool.map(one, args.repos) if r]
    repos.sort(key=lambda r: str(r["repo"]))
    all_prs = [p for r in repos for p in r["prs"] if p["author"] != "bot"]
    out: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "generated_at": until.isoformat(),
        "since": since.date().isoformat(),
        "until": until.date().isoformat(),
        "source": "GitHub REST API through the gh CLI, GET requests only. People are counted, never named.",
        "totals": {
            "repos": len(repos),
            "prs": len(all_prs),
            "ai_prs": sum(1 for p in all_prs if p["agents"]),
            "ai_reviewed_prs": sum(1 for p in all_prs if p["reviews"]["ai"]),
            "commits": sum(r["commits"]["total"] for r in repos),
            "ai_commits": sum(r["commits"]["ai"] for r in repos),
            "runs": sum(r["ci"]["runs"] for r in repos),
            "ai_runs": sum(r["ci"]["ai_runs"].get("any", 0) for r in repos),
            "minutes": round(sum(r["ci"]["minutes"] for r in repos), 1),
            "ai_minutes": round(sum(r["ci"]["ai_minutes"].get("any", 0) for r in repos), 1),
            "api_calls": gh.calls,
        },
        "agents": compare_agents(repos),
        "repos": repos,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False, default=str))
    t = out["totals"]
    print(
        f"wrote {args.out}: {t['prs']} PRs ({t['ai_prs']} AI), {t['ai_commits']}/{t['commits']} AI commits,"
        f" {t['ai_runs']}/{t['runs']} runs involve AI, {gh.calls} API calls",
        file=sys.stderr,
    )
    return 1 if incomplete else 0


if __name__ == "__main__":
    sys.exit(main())
