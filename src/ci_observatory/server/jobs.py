"""Analyses: a queue, a runner that executes the ci-observatory command in a subprocess, live progress, and cleanup.

Each analysis gets its own folder and its own cache. The subprocess sees only the token it needs, never the server's
other secrets. Public reports are kept and reused for a few hours; private ones belong to the session that asked for
them and are deleted soon after.
"""

import asyncio
import contextlib
import json
import os
import secrets
import shutil
import sys
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ci_observatory.server.config import Settings

FINAL = frozenset({"done", "failed", "cancelled"})
MAX_LINES = 2000
# An owner's analysis covers its busiest public repositories with CI in the window, up to these many. Each one costs
# about 80 API calls, and 10 of Aiven's took about 4 minutes, so owners also get twice the time of one repository.
OWNER_TOP_QUICK = 15
OWNER_TOP = 25
OWNER_MIN_RUNS = 3
#: Passed through to the subprocess; nothing else from the server's environment is.
SAFE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "SSL_CERT_FILE", "SSL_CERT_DIR")


@dataclass
class Job:
    id: str
    repo: str
    days: int
    private: bool
    owner: str
    quick: bool
    dir: Path
    status: str = "queued"
    lines: list[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    error: str | None = None
    proc: asyncio.subprocess.Process | None = None
    _changed: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def report(self) -> Path:
        return self.dir / "out" / "index.html"

    @property
    def whole_owner(self) -> bool:
        """An owner's public repositories together, rather than one repository."""
        return "/" not in self.repo

    def notify(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    async def changed(self, timeout: float) -> bool:
        event = self._changed
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except TimeoutError:
            return False
        return True

    def view(self, position: int | None = None) -> dict[str, Any]:
        return {
            "id": self.id,
            "repo": self.repo,
            "whole_owner": self.whole_owner,
            "days": self.days,
            "private": self.private,
            "quick": self.quick,
            "status": self.status,
            "position": position,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            "error": self.error,
            "report_url": f"/r/{self.id}" if self.status == "done" else None,
            "events_url": f"/api/analyses/{self.id}/events",
        }


class Limiter:
    """Sliding one-hour windows per key."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, per_hour: int) -> bool:
        now = time.time()
        hits = self._hits.setdefault(key, deque())
        while hits and hits[0] < now - 3600:
            hits.popleft()
        if len(hits) >= per_hour:
            return False
        hits.append(now)
        return True

    def forget_idle(self) -> None:
        cutoff = time.time() - 3600
        for key in [k for k, v in self._hits.items() if not v or v[-1] < cutoff]:
            del self._hits[key]


class Jobs:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.jobs: dict[str, Job] = {}
        self._slots = asyncio.Semaphore(settings.max_jobs)
        self._tasks: set[asyncio.Task[None]] = set()
        settings.data_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ lookup
    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def reusable(self, repo: str, days: int, quick: bool) -> Job | None:
        """A public analysis of the same repository and window that is running, or finished recently enough.
        A full analysis also answers a request for a quick one, but not the other way round."""
        fresh = time.time() - self.settings.public_cache_hours * 3600
        best: Job | None = None
        for job in self.jobs.values():
            if job.private or job.repo.lower() != repo.lower() or job.days != days or (job.quick and not quick):
                continue
            usable = job.status in ("queued", "running") or (job.status == "done" and (job.finished or 0) > fresh)
            if usable and (best is None or job.created > best.created):
                best = job
        return best

    def position(self, job: Job) -> int | None:
        if job.status != "queued":
            return None
        return sum(1 for j in self.jobs.values() if j.status == "queued" and j.created < job.created) + 1

    def waiting(self) -> int:
        return sum(1 for j in self.jobs.values() if j.status == "queued")

    def recent(self, limit: int = 8) -> list[dict[str, Any]]:
        done = [j for j in self.jobs.values() if not j.private and j.status == "done"]
        done.sort(key=lambda j: j.finished or 0, reverse=True)
        seen: set[tuple[str, int]] = set()
        rows: list[dict[str, Any]] = []
        for job in done:
            if (job.repo.lower(), job.days) in seen:
                continue
            seen.add((job.repo.lower(), job.days))
            rows.append(
                {
                    "repo": job.repo,
                    "whole_owner": job.whole_owner,
                    "days": job.days,
                    "finished": job.finished,
                    "report_url": f"/r/{job.id}",
                }
            )
        return rows[:limit]

    # ------------------------------------------------------------------ running
    def submit(self, repo: str, days: int, *, private: bool, owner: str, quick: bool, token: str) -> Job:
        job_id = secrets.token_urlsafe(12)
        job = Job(
            id=job_id, repo=repo, days=days, private=private, owner=owner, quick=quick, dir=self.settings.data_dir / job_id
        )
        job.dir.mkdir(parents=True)
        self.jobs[job_id] = job
        task = asyncio.create_task(self._run(job, token))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    def command(self, job: Job) -> Sequence[str]:
        """The analysis itself: the same command people run on their own machines."""
        cmd = [sys.executable, "-m", "ci_observatory.cli", "--days", str(job.days), "--out", str(job.dir / "out")]
        if job.whole_owner:
            top = OWNER_TOP_QUICK if job.quick else OWNER_TOP
            cmd += ["--org", job.repo, "--visibility", "public", "--top", str(top), "--min-runs", str(OWNER_MIN_RUNS)]
            cmd += ["--title", f"CI Observatory: {job.repo}'s public repositories"]
        else:
            cmd += ["--repo", job.repo, "--title", f"CI Observatory: {job.repo}"]
        cmd += ["--max-calls", "1500" if job.quick else "4000"]
        return [*cmd, "--quick"] if job.quick else cmd

    def environment(self, job: Job, token: str) -> dict[str, str]:
        env = {k: os.environ[k] for k in SAFE_ENV if k in os.environ}
        env.update(
            CI_OBSERVATORY_TRANSPORT="http",
            GH_TOKEN=token,
            XDG_CACHE_HOME=str(job.dir / "cache"),
            PYTHONUNBUFFERED="1",
        )
        return env

    def _clean(self, job: Job, line: str) -> str | None:
        """Progress lines without server paths, and without the hints meant for a terminal."""
        line = line.replace(str(job.dir / "out") + os.sep, "").replace(str(job.dir) + os.sep, "").rstrip()
        if line.startswith("Open it with:"):
            return None
        if " is private: its data stays in" in line:
            return f"  {job.repo} is private: the report is shown only to you, and deleted after you sign out."
        if line.startswith("Done in ") and ". Report:" in line:
            return line.split(". Report:", 1)[0] + "."
        return line

    def _add(self, job: Job, line: str) -> None:
        cleaned = self._clean(job, line)
        if cleaned is not None and len(job.lines) < MAX_LINES:
            job.lines.append(cleaned)
            job.notify()

    async def _run(self, job: Job, token: str) -> None:
        async with self._slots:
            if job.status == "cancelled":
                return
            job.status, job.started = "running", time.time()
            job.notify()
            limit = self.settings.job_timeout * (2 if job.whole_owner else 1)
            try:
                await asyncio.wait_for(self._execute(job, token), limit)
            except TimeoutError:
                await self._stop(job)
                job.status = "failed"
                job.error = f"The analysis took longer than {limit // 60} minutes and was stopped."
            except Exception as exc:  # the server must outlive any single analysis
                await self._stop(job)
                job.status, job.error = "failed", f"The analysis could not run: {exc}"
            finally:
                job.proc = None
                job.finished = time.time()
                shutil.rmtree(job.dir / "cache", ignore_errors=True)
                if job.status == "done" and not job.private:
                    self._save(job)
                job.notify()

    async def _execute(self, job: Job, token: str) -> None:
        job.proc = await asyncio.create_subprocess_exec(
            *self.command(job),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self.environment(job, token),
            cwd=job.dir,
        )
        assert job.proc.stdout is not None
        async for raw in job.proc.stdout:
            self._add(job, raw.decode("utf-8", "replace"))
        code = await job.proc.wait()
        if job.status == "cancelled":
            return
        if code in (0, 1) and job.report.exists():
            job.status = "done"
            return
        job.status = "failed"
        problems = [ln for ln in job.lines if ln.lstrip().startswith(("error:", "✕", "No repository"))]
        job.error = problems[-1].strip() if problems else f"The analysis stopped with exit code {code}."

    async def _stop(self, job: Job) -> None:
        if job.proc and job.proc.returncode is None:
            job.proc.terminate()
            try:
                await asyncio.wait_for(job.proc.wait(), 5)
            except TimeoutError:
                job.proc.kill()

    async def cancel(self, job: Job) -> None:
        if job.status in FINAL:
            return
        job.status, job.error = "cancelled", "Cancelled."
        await self._stop(job)
        job.notify()

    # ------------------------------------------------------------------ keeping and cleaning up
    def _save(self, job: Job) -> None:
        meta = {
            k: v for k, v in job.view().items() if k in ("id", "repo", "days", "quick", "created", "started", "finished")
        }
        (job.dir / "job.json").write_text(json.dumps(meta))

    def restore(self) -> None:
        """After a restart: keep fresh public reports, delete everything else (private reports have no owner now)."""
        fresh = time.time() - self.settings.public_cache_hours * 3600
        for path in self.settings.data_dir.iterdir() if self.settings.data_dir.exists() else []:
            meta_file = path / "job.json"
            try:
                meta = json.loads(meta_file.read_text()) if meta_file.exists() else None
            except (OSError, ValueError):
                meta = None
            if meta and meta.get("finished", 0) > fresh and (path / "out" / "index.html").exists():
                job = Job(
                    id=meta["id"],
                    repo=meta["repo"],
                    days=int(meta["days"]),
                    private=False,
                    owner="",
                    quick=bool(meta.get("quick")),
                    dir=path,
                    status="done",
                    created=meta["created"],
                    started=meta.get("started"),
                    finished=meta["finished"],
                )
                self.jobs[job.id] = job
            else:
                shutil.rmtree(path, ignore_errors=True)

    def sweep(self) -> None:
        """Delete expired reports: private ones after private_keep_minutes, public ones after public_cache_hours."""
        now = time.time()
        for job in list(self.jobs.values()):
            if job.status not in FINAL:
                continue
            keep = self.settings.private_keep_minutes * 60 if job.private else self.settings.public_cache_hours * 3600
            if job.status != "done":
                keep = min(keep, 3600)
            if (job.finished or job.created) < now - keep:
                self.drop(job)

    def drop(self, job: Job) -> None:
        self.jobs.pop(job.id, None)
        shutil.rmtree(job.dir, ignore_errors=True)

    def drop_owned(self, owner: str) -> None:
        """At sign-out: private reports of that session go at once."""
        for job in [j for j in self.jobs.values() if j.private and j.owner == owner]:
            if job.status in FINAL:
                self.drop(job)

    async def shutdown(self) -> None:
        for job in self.jobs.values():
            with contextlib.suppress(ProcessLookupError):
                await self._stop(job)
