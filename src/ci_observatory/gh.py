"""A small read-only GitHub client.

By default it shells out to the GitHub CLI: every request is `gh api --method GET ...`, so this package never sees or
stores a token. `gh` uses whatever you logged in with (`gh auth login`), or the GH_TOKEN / GITHUB_TOKEN environment
variable, and GH_HOST for GitHub Enterprise Server. Private repositories work the same way as public ones as long as
that login can read them.

Where `gh` is not installed but a token is in the environment (a container, the web server), it calls the REST API
directly over HTTPS instead, with the same GET-only interface. CI_OBSERVATORY_TRANSPORT=gh or =http forces either.
"""

import contextlib
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from threading import BoundedSemaphore, Lock
from typing import Any
from urllib.parse import urljoin, urlsplit

from ci_observatory import __version__

API_VERSION = "2022-11-28"
REDIRECTS = frozenset({301, 302, 303, 307, 308})


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


def host() -> str:
    return os.environ.get("GH_HOST", "").strip() or "github.com"


def env_token() -> str | None:
    """The token `gh` itself would use from the environment, in its order of precedence."""
    names = ("GH_TOKEN", "GITHUB_TOKEN")
    if host() != "github.com":
        names = ("GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
    return next((os.environ[n] for n in names if os.environ.get(n)), None)


def transport() -> str:
    """'gh' to run the GitHub CLI, 'http' to call the REST API directly with a token from the environment."""
    chosen = os.environ.get("CI_OBSERVATORY_TRANSPORT", "").strip().lower()
    if chosen in ("gh", "http"):
        return chosen
    return "http" if shutil.which("gh") is None and env_token() else "gh"


def unavailable() -> str | None:
    """Why GitHub cannot be reached at all, or None. A missing login shows up on the first request instead."""
    if transport() == "gh" and shutil.which("gh") is None:
        return (
            "the GitHub CLI `gh` is not installed (https://cli.github.com), and no GH_TOKEN is set to call the API directly"
        )
    return None


def api_base() -> str:
    return "https://api.github.com/" if host() == "github.com" else f"https://{host()}/api/v3/"


def seconds(duration: str) -> int:
    """'6h' -> 21600. Empty or malformed means no caching."""
    match = re.fullmatch(r"(\d+)([smhd])", duration.strip())
    return int(match.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)] if match else 0


class _Manual(urllib.request.HTTPRedirectHandler):
    """Redirects are followed by hand, so the token is only ever sent to the API host."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_Manual)


class Gh:
    """Counts calls, caps them, limits parallel requests and turns `gh` failures into exceptions."""

    def __init__(self, cache: str = "", max_calls: int = 5000, workers: int = 4) -> None:
        self.cache = cache
        self.max_calls = max_calls
        self.calls = 0
        self.transport = transport()
        self._token = env_token() if self.transport == "http" else None
        self._lock = Lock()
        self._slots = BoundedSemaphore(workers)

    def _run(self, path: str, params: dict[str, str | int] | None, accept: str, *, raw: bool = False) -> bytes:
        with self._lock:
            if self.calls >= self.max_calls:
                raise BudgetExceeded(None, f"reached --max-calls={self.max_calls}")
            self.calls += 1
        url = path + ("?" + "&".join(f"{k}={quote(str(v))}" for k, v in params.items()) if params else "")
        if self.transport == "http":
            with self._slots:
                return self._http(url, accept, raw=raw)
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

    def _cache_file(self, url: str, accept: str) -> Path | None:
        if not seconds(self.cache):
            return None
        who = hashlib.sha256((self._token or "anonymous").encode()).hexdigest()[:16]
        key = hashlib.sha256(f"{api_base()}\n{who}\n{accept}\n{url}".encode()).hexdigest()
        root = Path(os.environ.get("XDG_CACHE_HOME") or "~/.cache").expanduser()
        return root / "ci-observatory" / "http" / key[:2] / key

    def _http(self, path: str, accept: str, *, raw: bool) -> bytes:
        cached = None if raw else self._cache_file(path, accept)
        if cached and cached.exists() and cached.stat().st_mtime > time.time() - seconds(self.cache):
            return cached.read_bytes()
        url = urljoin(api_base(), path)
        api = urlsplit(api_base()).netloc
        for _ in range(5):
            headers = {"Accept": accept, "X-GitHub-Api-Version": API_VERSION, "User-Agent": f"ci-observatory/{__version__}"}
            on_api = urlsplit(url).netloc == api
            if self._token and on_api:
                headers["Authorization"] = f"Bearer {self._token}"
            if on_api:
                headers["Accept-Encoding"] = "gzip"  # a page of 100 runs: 1.3 MB of JSON, 0.07 MB gzipped
            try:
                with _OPENER.open(urllib.request.Request(url, headers=headers), timeout=600 if raw else 60) as response:
                    body: bytes = response.read()
                    if response.headers.get("Content-Encoding") == "gzip":
                        body = gzip.decompress(body)
                break
            except urllib.error.HTTPError as exc:
                location = exc.headers.get("Location")
                if exc.code in REDIRECTS and location:
                    # A log download redirects to blob storage, which must not see the GitHub token.
                    url = urljoin(url, location)
                    continue
                data = exc.read()
                if exc.headers.get("Content-Encoding") == "gzip":
                    with contextlib.suppress(OSError, EOFError):
                        data = gzip.decompress(data)
                text = data.decode("utf-8", "replace")[:500]
                message = f"HTTP {exc.code}: {text} ({path})"
                limited = exc.headers.get("x-ratelimit-remaining") == "0" or "rate limit" in text.lower()
                if exc.code in (403, 429) and limited:
                    raise RateLimited(exc.code, message) from None
                raise GhError(exc.code, message) from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise GhError(None, f"network error: {exc} ({path})") from None
        else:
            raise GhError(None, f"too many redirects ({path})")
        if cached:
            cached.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=cached.parent, delete=False) as tmp:
                tmp.write(body)
            os.replace(tmp.name, cached)
        return body

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
