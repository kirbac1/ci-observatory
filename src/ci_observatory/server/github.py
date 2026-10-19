"""The few GitHub calls the web server makes itself: the sign-in exchange, who is signed in, whether a repository or an
owner can be read, how much API quota is left, and revoking a token at sign-out. The analyses themselves run the
pipeline in a subprocess. These calls are blocking; the app runs them in a thread."""

import base64
import contextlib
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from ci_observatory import __version__
from ci_observatory.gh import API_VERSION
from ci_observatory.server.config import Settings


class GitHubError(Exception):
    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Repo:
    full_name: str
    private: bool
    owner_id: int


def _request(
    url: str, *, method: str = "GET", token: str | None = None, data: bytes | None = None, headers: dict[str, str]
) -> tuple[int, Any]:
    head = {"User-Agent": f"ci-observatory/{__version__}", **headers}
    if token:
        head["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, method=method, headers=head)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            return response.status, json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")[:300]
        return exc.code, {"message": text}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GitHubError(None, f"GitHub could not be reached: {exc}") from None


def _api(settings: Settings, path: str, token: str | None) -> tuple[int, Any]:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION}
    return _request(f"{settings.api_url}/{path}", token=token, headers=headers)


def authorize_url(settings: Settings, state: str, challenge: str) -> str:
    query = {
        "client_id": settings.client_id,
        "redirect_uri": settings.callback_url,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "allow_signup": "false",
    }
    if settings.oauth_scope:
        query["scope"] = settings.oauth_scope
    return f"{settings.github_url}/login/oauth/authorize?{urllib.parse.urlencode(query)}"


def exchange_code(settings: Settings, code: str, verifier: str) -> dict[str, Any]:
    """Trade the sign-in code for a user token. Raises GitHubError when GitHub refuses."""
    form = urllib.parse.urlencode(
        {
            "client_id": settings.client_id,
            "client_secret": settings.client_secret,
            "code": code,
            "redirect_uri": settings.callback_url,
            "code_verifier": verifier,
        }
    ).encode()
    status, body = _request(
        f"{settings.github_url}/login/oauth/access_token",
        method="POST",
        data=form,
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
    )
    if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
        reason = body.get("error_description") or body.get("error") if isinstance(body, dict) else None
        raise GitHubError(status, f"GitHub did not complete the sign-in: {reason or status}")
    return body


def user(settings: Settings, token: str) -> dict[str, Any]:
    status, body = _api(settings, "user", token)
    if status != 200 or not isinstance(body, dict):
        raise GitHubError(status, "could not read the signed-in account")
    return body


def repo(settings: Settings, name: str, token: str | None) -> Repo | None:
    """The repository as this token sees it, or None when it does not exist or the token cannot read it."""
    status, body = _api(settings, f"repos/{name}", token)
    if status in (403, 404) and not (isinstance(body, dict) and "rate limit" in str(body.get("message", "")).lower()):
        return None
    if status != 200 or not isinstance(body, dict):
        raise GitHubError(status, f"GitHub answered {status} for {name}")
    return Repo(full_name=body["full_name"], private=bool(body.get("private")), owner_id=int(body["owner"]["id"]))


def owner(settings: Settings, name: str, token: str | None) -> str | None:
    """The user or organisation's login as GitHub spells it, or None when there is no such account."""
    status, body = _api(settings, f"users/{name}", token)
    if status == 404:
        return None
    if status != 200 or not isinstance(body, dict):
        raise GitHubError(status, f"GitHub answered {status} for {name}")
    return str(body["login"])


def quota(settings: Settings, token: str | None) -> int | None:
    """Core API calls left this hour for the token, or None when GitHub does not say."""
    try:
        status, body = _api(settings, "rate_limit", token)
    except GitHubError:
        return None
    if status != 200 or not isinstance(body, dict):
        return None
    return int(body.get("resources", {}).get("core", {}).get("remaining", 0))


def revoke(settings: Settings, token: str) -> None:
    """Revoke a user token at sign-out. Best effort: a failure here must not keep someone signed in."""
    basic = base64.b64encode(f"{settings.client_id}:{settings.client_secret}".encode()).decode()
    with contextlib.suppress(GitHubError):
        _request(
            f"{settings.api_url}/applications/{settings.client_id}/token",
            method="DELETE",
            data=json.dumps({"access_token": token}).encode(),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": API_VERSION,
            },
        )
