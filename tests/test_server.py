"""The web app, with GitHub and the analysis command replaced by fakes, so these tests run offline in seconds."""

import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from ci_observatory.server import github
from ci_observatory.server.app import create_app, parse_repo
from ci_observatory.server.config import Settings
from ci_observatory.server.jobs import Job, Jobs

ORIGIN = {"Origin": "http://testserver"}
FAKE_ANALYSIS = """
import pathlib, sys
out = pathlib.Path(sys.argv[1])
print("[1/4] Workflow runs, jobs and hygiene", flush=True)
print("wrote " + str(out / "data" / "metrics.json"), flush=True)
out.mkdir(parents=True, exist_ok=True)
(out / "index.html").write_text("<!doctype html><html><head></head><body><h1>report</h1></body></html>")
print("[4/4] Report", flush=True)
print("Done in 1 s. Report: " + str(out / "index.html"), flush=True)
print("Open it with: open " + str(out / "index.html"), flush=True)
"""


class FakeGitHub:
    """Public "acme/api", private "acme/secret" (readable only with the user's token), nothing else."""

    def __init__(self) -> None:
        self.revoked: list[str] = []

    def repo(self, settings: Settings, name: str, token: str | None) -> github.Repo | None:
        if name.lower() == "acme/api":
            return github.Repo("acme/api", private=False, owner_id=1)
        if name.lower() == "acme/secret" and token in ("user-token", "server-token"):
            return github.Repo("acme/secret", private=True, owner_id=1)
        return None

    def exchange_code(self, settings: Settings, code: str, verifier: str) -> dict[str, Any]:
        if code != "good-code":
            raise github.GitHubError(400, "bad code")
        return {"access_token": "user-token", "expires_in": 28800}

    def user(self, settings: Settings, token: str) -> dict[str, Any]:
        return {"login": "octo", "avatar_url": "https://avatars.githubusercontent.com/u/1"}

    def revoke(self, settings: Settings, token: str) -> None:
        self.revoked.append(token)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    gh = FakeGitHub()
    for name in ("repo", "exchange_code", "user", "revoke"):
        monkeypatch.setattr(github, name, getattr(gh, name))
    monkeypatch.setattr(github, "quota", lambda settings, token: 5000)
    monkeypatch.setattr(Jobs, "command", lambda self, job: [sys.executable, "-c", FAKE_ANALYSIS, str(job.dir / "out")])
    return gh


def settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "public_url": "http://testserver",
        "client_id": "client-id",
        "client_secret": "client-secret",
        "app_slug": "ci-observatory",
        "server_token": "server-token",
        "data_dir": tmp_path / "data",
    }
    return Settings(**{**values, **overrides})


@pytest.fixture
def client(tmp_path: Path, fake: FakeGitHub) -> Iterator[TestClient]:
    with TestClient(create_app(settings(tmp_path))) as c:
        yield c


def wait_done(client: TestClient, job_id: str) -> dict[str, Any]:
    for _ in range(100):
        view: dict[str, Any] = client.get(f"/api/analyses/{job_id}").json()
        if view["status"] in ("done", "failed", "cancelled"):
            return view
        time.sleep(0.05)
    raise AssertionError("the analysis did not finish")


def sign_in(client: TestClient) -> None:
    login = client.get("/login", params={"next": "/?repo=acme/secret"}, follow_redirects=False)
    state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
    done = client.get("/auth/callback", params={"code": "good-code", "state": state}, follow_redirects=False)
    assert done.status_code == 303 and done.headers["location"] == "/?repo=acme/secret"


def test_parse_repo_accepts_names_and_github_links() -> None:
    assert parse_repo("cli/cli") == "cli/cli"
    assert parse_repo(" https://github.com/aiven/aiven-client/actions?query=x ") == "aiven/aiven-client"
    assert parse_repo("github.com/astral-sh/uv.git") == "astral-sh/uv"
    assert parse_repo("not a repo") is None
    assert parse_repo("../etc/passwd") is None
    assert parse_repo("https://gitlab.com/a/b") is None


def test_settings_come_from_the_environment(tmp_path: Path) -> None:
    env = {"CIO_PUBLIC_URL": "https://obs.example", "CIO_MAX_JOBS": "3", "CIO_PUBLIC_CACHE_HOURS": "1.5"}
    s = Settings.from_env({**env, "CIO_DATA_DIR": str(tmp_path)})
    assert (s.public_url, s.max_jobs, s.public_cache_hours, s.data_dir) == ("https://obs.example", 3, 1.5, tmp_path)
    assert s.secure_cookies and s.origin == "https://obs.example" and not s.login_enabled


def test_a_public_analysis_runs_streams_and_is_reused(client: TestClient) -> None:
    started = client.post("/api/analyses", json={"repo": "https://github.com/acme/api", "days": 30}, headers=ORIGIN)
    assert started.status_code == 202, started.text
    job = started.json()
    assert job["repo"] == "acme/api" and job["quick"] is True  # not signed in: smaller samples

    with client.stream("GET", job["events_url"]) as events:
        stream = "".join(events.iter_text())
    assert "[1/4] Workflow runs" in stream and '"status": "done"' in stream
    assert "wrote data/metrics.json" in stream
    assert str(client.app.state.settings.data_dir) not in stream  # type: ignore[attr-defined]
    assert "Open it with" not in stream and "Done in 1 s." in stream

    view = wait_done(client, job["id"])
    assert view["report_url"] == f"/r/{job['id']}"
    assert any(line == "wrote data/metrics.json" for line in view["lines"])
    page = client.get(view["report_url"])
    assert page.status_code == 200 and "<h1>report</h1>" in page.text and "Analyse another repository" in page.text
    assert page.headers["x-robots-tag"] == "noindex" and "public" in page.headers["cache-control"]

    again = client.post("/api/analyses", json={"repo": "acme/api", "days": 30}, headers=ORIGIN)
    assert again.status_code == 200 and again.json()["id"] == job["id"]  # the fresh public report is reused
    assert client.get("/api/recent").json()[0]["repo"] == "acme/api"


def test_requests_from_other_sites_are_refused(client: TestClient) -> None:
    assert client.post("/api/analyses", json={"repo": "acme/api"}).status_code == 403
    assert (
        client.post("/api/analyses", json={"repo": "acme/api"}, headers={"Origin": "https://evil.example"}).status_code
        == 403
    )
    assert client.post("/logout", headers={"Origin": "https://evil.example"}).status_code == 403


def test_without_signing_in_private_repositories_look_missing(client: TestClient) -> None:
    # The server token could read it, but a visitor who is not signed in must never get a report of it.
    refused = client.post("/api/analyses", json={"repo": "acme/secret"}, headers=ORIGIN)
    assert refused.status_code == 404 and refused.json()["login"] is True
    assert "private" not in refused.json()["error"].lower().replace("if it is private", "")
    assert client.app.state.jobs.jobs == {}  # type: ignore[attr-defined]


def test_sign_in_uses_state_and_pkce_and_a_new_session(client: TestClient) -> None:
    login = client.get("/login", follow_redirects=False)
    query = parse_qs(urlsplit(login.headers["location"]).query)
    assert login.headers["location"].startswith("https://github.com/login/oauth/authorize?")
    assert query["code_challenge_method"] == ["S256"] and query["redirect_uri"] == ["http://testserver/auth/callback"]
    assert "cio_state" in login.headers["set-cookie"] and "HttpOnly" in login.headers["set-cookie"]

    forged = client.get("/auth/callback", params={"code": "good-code", "state": "someone-elses"}, follow_redirects=False)
    assert forged.headers["location"] == "/?error=expired"
    assert client.get("/api/me").json()["signed_in"] is False

    before = client.cookies.get("cio_session")
    sign_in(client)
    me = client.get("/api/me").json()
    assert me["signed_in"] is True and me["login"] == "octo" and "token" not in str(me)
    assert client.cookies.get("cio_session") not in (None, before)


def test_private_reports_belong_to_their_owner_and_go_at_sign_out(client: TestClient, fake: FakeGitHub) -> None:
    sign_in(client)
    started = client.post("/api/analyses", json={"repo": "acme/secret", "days": 7}, headers=ORIGIN)
    assert started.status_code == 202 and started.json()["private"] is True and started.json()["quick"] is False
    job = wait_done(client, started.json()["id"])
    own = client.get(job["report_url"])
    assert own.status_code == 200 and own.headers["cache-control"] == "private, no-store"

    stranger = TestClient(client.app)
    assert stranger.get(job["report_url"]).status_code == 404
    assert stranger.get(f"/api/analyses/{job['id']}").status_code == 404
    assert client.get("/api/recent").json() == []  # private reports are never listed

    assert client.post("/logout", headers=ORIGIN).status_code == 200
    assert fake.revoked == ["user-token"]
    assert client.get(job["report_url"]).status_code == 404
    assert not Path(client.app.state.settings.data_dir / job["id"]).exists()  # type: ignore[attr-defined]


def test_visitors_who_are_not_signed_in_are_rate_limited(tmp_path: Path, fake: FakeGitHub) -> None:
    with TestClient(create_app(settings(tmp_path, anon_per_hour=1))) as client:
        assert client.post("/api/analyses", json={"repo": "acme/api", "days": 7}, headers=ORIGIN).status_code == 202
        limited = client.post("/api/analyses", json={"repo": "acme/api", "days": 14}, headers=ORIGIN)
        assert limited.status_code == 429 and limited.json()["login"] is True


def test_without_a_server_token_people_must_sign_in(tmp_path: Path, fake: FakeGitHub) -> None:
    with TestClient(create_app(settings(tmp_path, server_token=""))) as client:
        answer = client.post("/api/analyses", json={"repo": "acme/api"}, headers=ORIGIN)
        assert answer.status_code == 401 and answer.json()["login"] is True


def test_the_analysis_never_sees_the_servers_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CIO_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("CIO_SERVER_TOKEN", "server-token")
    jobs = Jobs(settings(tmp_path))
    job = Job(id="j", repo="acme/api", days=7, private=False, owner="s", quick=True, dir=tmp_path / "j")
    env = jobs.environment(job, "user-token")
    assert env["GH_TOKEN"] == "user-token" and env["CI_OBSERVATORY_TRANSPORT"] == "http"
    assert not any(key.startswith("CIO_") for key in env) and "client-secret" not in env.values()


def test_progress_lines_hide_server_paths_and_terminal_hints(tmp_path: Path) -> None:
    jobs = Jobs(settings(tmp_path))
    job = Job(id="j", repo="acme/secret", days=7, private=True, owner="s", quick=False, dir=tmp_path / "j")
    out = str(job.dir / "out")
    assert jobs._clean(job, f"wrote {out}/data/runs.json (4 rows)") == "wrote data/runs.json (4 rows)"
    assert jobs._clean(job, f"Done in 70 s. Report: {out}/index.html") == "Done in 70 s."
    assert jobs._clean(job, f"Open it with: open {out}/index.html") is None
    private = jobs._clean(job, f"  acme/secret is private: its data stays in {out}/ on this machine.")
    assert private is not None and "shown only to you" in private and out not in private


def test_other_addresses_of_the_service_redirect_to_the_public_one(client: TestClient) -> None:
    moved = client.get("/r/abc", headers={"Host": "ci-observatory-abc-lz.a.run.app"}, follow_redirects=False)
    assert moved.status_code == 301 and moved.headers["location"] == "http://testserver/r/abc"
    assert client.get("/health").json()["ok"] is True
