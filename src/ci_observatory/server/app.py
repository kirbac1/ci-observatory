"""The web app: one page to start an analysis, live progress, the report, and signing in with GitHub.

Security model, in short:
- The GitHub token never reaches the browser. Sessions are random IDs in an httpOnly cookie; the token stays in this
  process's memory and is revoked at sign-out.
- People who are not signed in can only analyse public repositories, read with the server's own token. A private
  repository answers "not found" to them, even when the server's token could read it.
- Private reports are visible only to the session that asked for them and are deleted at sign-out or soon after.
- State-changing requests must come from this app's own origin; sign-in uses a state cookie and PKCE.
"""

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from importlib import resources
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from ci_observatory.cli import REPO
from ci_observatory.server import github
from ci_observatory.server.config import Settings
from ci_observatory.server.jobs import FINAL, Job, Jobs, Limiter

log = logging.getLogger("ci_observatory.server")

DAYS = (7, 14, 30, 60, 90)
SESSION_COOKIE = "cio_session"
STATE_COOKIE = "cio_state"
SESSION_HOURS = 8
STATIC = {"app.css": "text/css", "app.js": "text/javascript", "favicon.svg": "image/svg+xml"}
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
    "font-src https://fonts.gstatic.com; img-src 'self' data: https://avatars.githubusercontent.com; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
GITHUB_URL = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/([^/\s]+)/([^/\s#?]+)", re.I)


@dataclass
class Session:
    id: str
    seen: float
    token: str | None = None
    expires: float = 0.0
    login: str | None = None
    avatar: str | None = None

    @property
    def signed_in(self) -> bool:
        return bool(self.token) and self.expires > time.time()


class Sessions:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.items: dict[str, Session] = {}

    def get(self, request: Request) -> Session | None:
        session = self.items.get(request.cookies.get(SESSION_COOKIE, ""))
        if session is None:
            return None
        if session.token and session.expires <= time.time():
            session.token = None  # GitHub's user tokens expire; the person signs in again
        session.seen = time.time()
        return session

    def create(self, **values: Any) -> Session:
        session = Session(id=secrets.token_urlsafe(32), seen=time.time(), **values)
        self.items[session.id] = session
        return session

    def remove(self, session: Session) -> None:
        self.items.pop(session.id, None)

    def cookie(self, response: Response, session: Session) -> None:
        response.set_cookie(
            SESSION_COOKIE,
            session.id,
            max_age=SESSION_HOURS * 3600,
            httponly=True,
            secure=self.settings.secure_cookies,
            samesite="lax",
        )

    def sweep(self) -> None:
        cutoff = time.time() - 12 * 3600
        for key in [k for k, s in self.items.items() if s.seen < cutoff]:
            del self.items[key]


class AnalysisIn(BaseModel):
    repo: str = Field(max_length=300)
    days: int = 30


def parse_repo(text: str) -> str | None:
    """owner/name from "owner/name", "github.com/owner/name" or a full GitHub URL, or None."""
    text = text.strip()
    match = GITHUB_URL.match(text)
    name = f"{match.group(1)}/{match.group(2)}" if match else text
    name = name.removesuffix("/").removesuffix(".git")
    return name if REPO.match(name) and len(name) <= 140 else None


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def safe_next(value: str | None) -> str:
    """Only paths on this site, so the sign-in cannot be used to send people elsewhere."""
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/"


def problem(status: int, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    jobs = Jobs(settings)
    sessions = Sessions(settings)
    limiter = Limiter()
    pending: dict[str, tuple[str, str, float]] = {}  # sign-in state -> (PKCE verifier, where to go next, created)

    async def sweeper() -> None:
        while True:
            await asyncio.sleep(60)
            jobs.sweep()
            sessions.sweep()
            limiter.forget_idle()
            for state in [s for s, (_, _, t) in pending.items() if t < time.time() - 600]:
                pending.pop(state, None)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        jobs.restore()
        task = asyncio.create_task(sweeper())
        try:
            yield
        finally:
            task.cancel()
            await jobs.shutdown()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.jobs, app.state.sessions = settings, jobs, sessions

    @app.middleware("http")
    async def headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        started = time.monotonic()
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        # The path only: a query string can hold a sign-in code.
        log.info(
            "%s %s %s %.0fms", request.method, request.url.path, response.status_code, (time.monotonic() - started) * 1e3
        )
        return response

    def same_origin(request: Request) -> None:
        if request.headers.get("origin") != settings.origin:
            raise HTTPException(403, "This request must come from the CI Observatory page.")

    def visible(job: Job | None, session: Session | None) -> Job:
        if job is None or (job.private and (session is None or session.id != job.owner)):
            raise HTTPException(404, "No such analysis.")
        return job

    # ------------------------------------------------------------------ pages
    @app.get("/", include_in_schema=False)
    async def index() -> Response:
        path = resources.files("ci_observatory.server").joinpath("static/index.html")
        return Response(
            path.read_bytes(), media_type="text/html", headers={"Content-Security-Policy": CSP, "Cache-Control": "no-cache"}
        )

    @app.get("/static/{name}", include_in_schema=False)
    async def static(name: str) -> Response:
        if name not in STATIC:
            raise HTTPException(404)
        body = resources.files("ci_observatory.server").joinpath(f"static/{name}").read_bytes()
        return Response(body, media_type=STATIC[name], headers={"Cache-Control": "public, max-age=300"})

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, Any]:
        running = sum(1 for j in jobs.jobs.values() if j.status == "running")
        return {"ok": True, "running": running, "queued": jobs.waiting()}

    @app.get("/r/{job_id}", include_in_schema=False)
    async def report(job_id: str, request: Request) -> Response:
        job = visible(jobs.get(job_id), sessions.get(request))
        if job.status != "done" or not job.report.exists():
            raise HTTPException(404, "This report is not ready, or it has expired.")
        html = job.report.read_text(encoding="utf-8")
        at = html.find(">", html.find("<body")) + 1
        back = (
            '<a href="/" style="position:fixed;right:16px;bottom:16px;z-index:99;padding:9px 15px;border-radius:999px;'
            "background:#5fd3f3;color:#04131a;font:600 14px/1 system-ui,sans-serif;text-decoration:none;"
            'box-shadow:0 6px 24px rgba(0,0,0,.4)">Analyse another repository</a>'
        )
        cache = "private, no-store" if job.private else "public, max-age=300"
        return Response(
            html[:at] + back + html[at:],
            media_type="text/html",
            headers={"Cache-Control": cache, "X-Robots-Tag": "noindex", "Content-Security-Policy": "frame-ancestors 'none'"},
        )

    # ------------------------------------------------------------------ API
    @app.get("/api/me")
    async def me(request: Request) -> dict[str, Any]:
        session = sessions.get(request)
        signed_in = bool(session and session.signed_in)
        return {
            "signed_in": signed_in,
            "login": session.login if session and signed_in else None,
            "avatar": session.avatar if session and signed_in else None,
            "login_enabled": settings.login_enabled,
            "anonymous": bool(settings.server_token),
            "install_url": settings.install_url,
            "days": DAYS,
        }

    @app.get("/api/recent")
    async def recent() -> list[dict[str, Any]]:
        return jobs.recent()

    @app.post("/api/analyses")
    async def start(body: AnalysisIn, request: Request) -> Response:
        same_origin(request)
        name = parse_repo(body.repo)
        if not name:
            return problem(400, "Give a repository as owner/name, or paste its GitHub address.")
        if body.days not in DAYS:
            return problem(400, f"Pick one of {', '.join(map(str, DAYS))} days.")
        session = sessions.get(request)
        signed_in = bool(session and session.signed_in)
        token = session.token if session and signed_in else settings.server_token
        if not token:
            return problem(401, "Sign in with GitHub to analyse a repository.", login=settings.login_enabled)
        try:
            found = await asyncio.to_thread(github.repo, settings, name, token)
        except github.GitHubError as exc:
            if exc.status in (403, 429):
                return problem(
                    503, "GitHub's rate limit is used up for now. Try again later, or sign in.", login=not signed_in
                )
            return problem(502, "GitHub could not be reached. Try again in a moment.")
        if found is None or (found.private and not signed_in):
            if signed_in:
                hint = "For a private repository, install the app on its owner and give it access to this repository."
                return problem(
                    404, f"{name} was not found, or CI Observatory cannot see it. {hint}", install_url=settings.install_url
                )
            hint = " If it is private, sign in with GitHub." if settings.login_enabled else ""
            return problem(404, f"{name} was not found.{hint}", login=settings.login_enabled)

        quick = not signed_in  # the shared server token stretches further with smaller samples
        if not found.private and (reuse := jobs.reusable(found.full_name, body.days, quick)):
            return JSONResponse(reuse.view(jobs.position(reuse)))

        fresh = session is None
        if session is None:
            session = sessions.create()
        if signed_in:
            if not limiter.allow(f"user:{session.login}", settings.user_per_hour):
                return problem(429, "That is a lot of analyses for one hour. Try again a little later.")
        else:
            ok = limiter.allow(f"ip:{client_ip(request)}", settings.anon_per_hour) and limiter.allow(
                "anonymous", settings.anon_total_per_hour
            )
            if not ok:
                return problem(
                    429,
                    "Too many analyses without signing in. Sign in with GitHub to use your own quota.",
                    login=settings.login_enabled,
                )
            left = await asyncio.to_thread(github.quota, settings, settings.server_token)
            if left is not None and left < settings.min_server_quota:
                return problem(
                    503,
                    "The shared GitHub quota is used up for this hour. Sign in to use your own.",
                    login=settings.login_enabled,
                )
        if jobs.waiting() >= settings.queue_limit:
            return problem(503, "Many analyses are waiting already. Try again in a few minutes.")

        job = jobs.submit(found.full_name, body.days, private=found.private, owner=session.id, quick=quick, token=token)
        response = JSONResponse(job.view(jobs.position(job)), status_code=202)
        if fresh:
            sessions.cookie(response, session)
        return response

    @app.get("/api/analyses/{job_id}")
    async def status(job_id: str, request: Request) -> dict[str, Any]:
        job = visible(jobs.get(job_id), sessions.get(request))
        return {**job.view(jobs.position(job)), "lines": job.lines[-200:]}

    @app.delete("/api/analyses/{job_id}")
    async def cancel(job_id: str, request: Request) -> dict[str, Any]:
        same_origin(request)
        session = sessions.get(request)
        job = visible(jobs.get(job_id), session)
        if session is None or job.owner != session.id:
            raise HTTPException(403, "Only the person who started an analysis can cancel it.")
        await jobs.cancel(job)
        return job.view()

    @app.get("/api/analyses/{job_id}/events")
    async def events(job_id: str, request: Request) -> StreamingResponse:
        job = visible(jobs.get(job_id), sessions.get(request))

        async def stream() -> AsyncIterator[str]:
            sent, last = 0, None
            while True:
                while sent < len(job.lines):
                    yield f"event: line\ndata: {json.dumps(job.lines[sent])}\n\n"
                    sent += 1
                view = job.view(jobs.position(job))
                if view != last:
                    yield f"event: status\ndata: {json.dumps(view)}\n\n"
                    last = view
                if job.status in FINAL:
                    return
                if not await job.changed(15):
                    yield ": still working\n\n"
                if await request.is_disconnected():
                    return

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        )

    # ------------------------------------------------------------------ signing in
    @app.get("/login", include_in_schema=False)
    async def login(next: str | None = None) -> Response:
        if not settings.login_enabled:
            return RedirectResponse("/?error=login-off", 303)
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        if len(pending) > 5000:
            pending.clear()
        pending[state] = (verifier, safe_next(next), time.time())
        response = RedirectResponse(github.authorize_url(settings, state, challenge), 302)
        response.set_cookie(
            STATE_COOKIE, state, max_age=600, httponly=True, secure=settings.secure_cookies, samesite="lax", path="/auth"
        )
        return response

    @app.get("/auth/callback", include_in_schema=False)
    async def callback(request: Request, code: str = "", state: str = "", error: str = "") -> Response:
        if error:
            return RedirectResponse("/?error=denied", 303)
        entry = pending.pop(state, None) if state else None
        cookie = request.cookies.get(STATE_COOKIE)
        if (
            entry is None
            or not code
            or not cookie
            or not secrets.compare_digest(cookie, state)
            or entry[2] < time.time() - 600
        ):
            return RedirectResponse("/?error=expired", 303)
        verifier, target, _ = entry
        try:
            granted = await asyncio.to_thread(github.exchange_code, settings, code, verifier)
            who = await asyncio.to_thread(github.user, settings, granted["access_token"])
        except github.GitHubError:
            return RedirectResponse("/?error=github", 303)
        old = sessions.get(request)
        if old:
            sessions.remove(old)  # a new session ID at every sign-in
        lifetime = min(int(granted.get("expires_in") or SESSION_HOURS * 3600), SESSION_HOURS * 3600)
        session = sessions.create(
            token=granted["access_token"],
            expires=time.time() + lifetime,
            login=who.get("login"),
            avatar=who.get("avatar_url"),
        )
        response = RedirectResponse(target, 303)
        sessions.cookie(response, session)
        response.delete_cookie(STATE_COOKIE, path="/auth")
        return response

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        same_origin(request)
        session = sessions.get(request)
        if session:
            for job in [j for j in jobs.jobs.values() if j.private and j.owner == session.id]:
                await jobs.cancel(job)
            jobs.drop_owned(session.id)
            if session.token and settings.login_enabled:
                await asyncio.to_thread(github.revoke, settings, session.token)
            sessions.remove(session)
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE)
        return response

    return app
