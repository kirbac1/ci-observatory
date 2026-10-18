"""A small read-only GitHub client that shells out to the GitHub CLI.

Every request is `gh api --method GET ...`, so this package never sees or stores a token: `gh` uses whatever you logged
in with (`gh auth login`), or the GH_TOKEN / GITHUB_TOKEN environment variable, and GH_HOST for GitHub Enterprise
Server. Private repositories work the same way as public ones as long as that login can read them.
"""

import json
import re
import subprocess
from threading import BoundedSemaphore, Lock
from typing import Any

API_VERSION = "2022-11-28"


class GhError(Exception):
    """A `gh api` call failed."""

    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status


class RateLimited(GhError):
    """GitHub says slow down. Stop and keep what we have."""


class BudgetExceeded(GhError):
    """The `--max-calls` safety limit was reached."""


def quote(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-._~" else f"%{ord(c):02X}" for c in value)


class Gh:
    """Counts calls, caps them, limits parallel requests and turns `gh` failures into exceptions."""

    def __init__(self, cache: str = "", max_calls: int = 5000, workers: int = 4) -> None:
        self.cache = cache
        self.max_calls = max_calls
        self.calls = 0
        self._lock = Lock()
        self._slots = BoundedSemaphore(workers)

    def _run(self, path: str, params: dict[str, str | int] | None, accept: str, *, raw: bool = False) -> bytes:
        with self._lock:
            if self.calls >= self.max_calls:
                raise BudgetExceeded(None, f"reached --max-calls={self.max_calls}")
            self.calls += 1
        url = path + ("?" + "&".join(f"{k}={quote(str(v))}" for k, v in params.items()) if params else "")
        cmd = ["gh", "api", "--method", "GET", "-H", f"Accept: {accept}", "-H", f"X-GitHub-Api-Version: {API_VERSION}"]
        if self.cache and not raw:
            cmd += ["--cache", self.cache]
        if raw:
            # Job logs carry terminal colour codes. They go to a file and the parser strips them, never to a terminal.
            cmd.append("--allow-escape-sequences")
        with self._slots:
            proc = subprocess.run([*cmd, "--", url], capture_output=True, timeout=600, check=False)
        if proc.returncode != 0:
            text = (proc.stderr + proc.stdout).decode("utf-8", "replace").strip()[:500]
            match = re.search(r"HTTP (\d{3})", text)
            status = int(match.group(1)) if match else None
            if status in (403, 429) and "rate limit" in text.lower():
                raise RateLimited(status, text)
            raise GhError(status, text)
        return proc.stdout

    def json(self, path: str, params: dict[str, str | int] | None = None) -> Any:
        """GET a JSON document. Returns None when the resource does not exist."""
        try:
            return json.loads(self._run(path, params, "application/vnd.github+json") or b"null")
        except GhError as exc:
            if exc.status == 404 and not isinstance(exc, BudgetExceeded):
                return None
            raise

    def text(self, path: str) -> str | None:
        """GET a file's raw contents. Returns None when it does not exist."""
        try:
            return self._run(path, None, "application/vnd.github.raw+json").decode("utf-8", "replace")
        except GhError as exc:
            if exc.status == 404 and not isinstance(exc, BudgetExceeded):
                return None
            raise

    def raw(self, path: str) -> bytes:
        """GET a binary or text download such as a job log. Raises GhError when it is missing or expired."""
        return self._run(path, None, "application/vnd.github+json", raw=True)
