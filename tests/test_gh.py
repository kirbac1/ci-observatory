"""The GitHub client's direct HTTPS transport, against a local fake API."""

import gzip
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ci_observatory import gh

SEEN: list[tuple[str, str | None]] = []


class FakeApi(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        SEEN.append((f"{self.server.server_port}{self.path}", self.headers.get("Authorization")))  # type: ignore[attr-defined]
        if self.path == "/repos/acme/api":
            self.reply(200, json.dumps({"full_name": "acme/api"}).encode())
        elif self.path == "/repos/acme/api/actions/jobs/1/logs":
            # GitHub sends log downloads to blob storage on another host.
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{STORAGE.server_port}/blob/1?sig=x")
            self.end_headers()
        elif self.path.startswith("/blob/1"):
            self.reply(200, b"2026-10-19T00:00:00Z log line\n")
        elif self.path == "/limited":
            self.send_response(403)
            self.send_header("x-ratelimit-remaining", "0")
            self.end_headers()
            self.wfile.write(b'{"message": "API rate limit exceeded"}')
        elif self.path == "/secondary":
            # A secondary rate limit says so only in the (here gzipped) body.
            body = gzip.compress(b'{"message": "You have exceeded a secondary rate limit."}')
            self.send_response(403)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.reply(404, b'{"message": "Not Found"}')

    def reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


API = ThreadingHTTPServer(("127.0.0.1", 0), FakeApi)
STORAGE = ThreadingHTTPServer(("127.0.0.1", 0), FakeApi)


@pytest.fixture(scope="module", autouse=True)
def servers() -> Iterator[None]:
    for server in (API, STORAGE):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    yield
    for server in (API, STORAGE):
        server.shutdown()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> gh.Gh:
    monkeypatch.setattr(gh, "api_base", lambda: f"http://127.0.0.1:{API.server_port}/")
    monkeypatch.setenv("CI_OBSERVATORY_TRANSPORT", "http")
    monkeypatch.setenv("GH_TOKEN", "secret-token")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    SEEN.clear()
    return gh.Gh(cache="1h", max_calls=20)


def test_json_text_and_missing(client: gh.Gh) -> None:
    assert client.json("repos/acme/api") == {"full_name": "acme/api"}
    assert client.json("repos/acme/nothing") is None
    assert SEEN[0][1] == "Bearer secret-token"


def test_responses_are_cached_per_token(client: gh.Gh, tmp_path: Path) -> None:
    client.json("repos/acme/api")
    client.json("repos/acme/api")
    assert len(SEEN) == 1 and any((tmp_path / "ci-observatory" / "http").rglob("*"))


def test_a_log_redirect_never_carries_the_token_to_storage(client: gh.Gh) -> None:
    assert client.raw("repos/acme/api/actions/jobs/1/logs").endswith(b"log line\n")
    to_storage = [auth for where, auth in SEEN if where.startswith(str(STORAGE.server_port))]
    assert to_storage == [None]


def test_rate_limits_are_recognised(client: gh.Gh) -> None:
    with pytest.raises(gh.RateLimited):
        client.json("limited")
    with pytest.raises(gh.RateLimited):
        client.json("secondary")


def test_gh_is_only_required_when_it_is_the_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gh.shutil, "which", lambda name: None)
    monkeypatch.delenv("CI_OBSERVATORY_TRANSPORT", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert gh.transport() == "gh" and gh.unavailable() is not None
    monkeypatch.setenv("GH_TOKEN", "t")
    assert gh.transport() == "http" and gh.unavailable() is None


def test_durations() -> None:
    assert gh.seconds("6h") == 21600
    assert gh.seconds("90s") == 90
    assert gh.seconds("1d") == 86400
    assert gh.seconds("") == gh.seconds("soon") == 0
