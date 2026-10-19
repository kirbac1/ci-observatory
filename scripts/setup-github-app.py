#!/usr/bin/env python3
"""Connect a deployed CI Observatory web app to GitHub, in two clicks in your browser.

    python3 scripts/setup-github-app.py --public-url https://ci-observatory-....run.app \\
        --cloud-run ci-observatory --region europe-north1 --project my-project

It serves a small page on http://127.0.0.1:8799 with two steps:

1. Create the GitHub App. The page sends GitHub a ready-made app description (read-only Actions, Contents and Pull
   requests; the app's callback and setup URLs), you confirm on github.com, and GitHub hands this script the app's
   client ID and secret.
2. Paste a fine-grained token that can read public repositories only. It serves people who are not signed in. The
   script refuses a token that can see any private repository.

With --cloud-run, both secrets go straight into Secret Manager and onto the Cloud Run service; nothing is printed.
Without it, they are written to ./ci-observatory.env (readable by you only) for any other host.
The app's private key is not needed and is discarded.
"""

import argparse
import html
import json
import os
import secrets
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PORT = 8799
API = "https://api.github.com"


def github(path: str, *, method: str = "GET", token: str | None = None) -> tuple[int, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "ci-observatory-setup",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{API}/{path}", method=method, headers=headers, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, None


class Setup:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.state = secrets.token_urlsafe(16)
        self.app: dict[str, Any] | None = None
        self.token_ok = False
        self.done = threading.Event()

    # ------------------------------------------------------------------ storing
    def gcloud(self, *cmd: str, stdin: str | None = None) -> None:
        full = ["gcloud", *cmd, "--project", self.args.project, "--quiet"]
        proc = subprocess.run(full, input=stdin, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"gcloud {cmd[0]} {cmd[1]} failed: {proc.stderr.strip()[:300]}")

    def secret(self, name: str, value: str) -> None:
        exists = (
            subprocess.run(
                ["gcloud", "secrets", "describe", name, "--project", self.args.project], capture_output=True, check=False
            ).returncode
            == 0
        )
        if exists:
            self.gcloud("secrets", "versions", "add", name, "--data-file=-", stdin=value)
        else:
            self.gcloud("secrets", "create", name, "--data-file=-", stdin=value)
        number = subprocess.run(
            ["gcloud", "projects", "describe", self.args.project, "--format=value(projectNumber)"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        account = f"serviceAccount:{number}-compute@developer.gserviceaccount.com"
        self.gcloud(
            "secrets", "add-iam-policy-binding", name, f"--member={account}", "--role=roles/secretmanager.secretAccessor"
        )

    def store(self, env: dict[str, str], secret_env: dict[str, tuple[str, str]]) -> None:
        """env: plain variables. secret_env: variable -> (secret name, value)."""
        if self.args.cloud_run:
            for _, (name, value) in secret_env.items():
                self.secret(name, value)
            cmd = ["run", "services", "update", self.args.cloud_run, "--region", self.args.region]
            if env:
                cmd.append("--update-env-vars=" + ",".join(f"{k}={v}" for k, v in env.items()))
            if secret_env:
                cmd.append("--update-secrets=" + ",".join(f"{k}={n}:latest" for k, (n, _) in secret_env.items()))
            self.gcloud(*cmd)
            return
        path = "ci-observatory.env"
        lines = [f"{k}={v}" for k, v in env.items()] + [f"{k}={v}" for k, (_, v) in secret_env.items()]
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write("\n".join(lines) + "\n")

    # ------------------------------------------------------------------ steps
    def manifest(self) -> str:
        base = self.args.public_url.rstrip("/")
        return json.dumps(
            {
                "name": self.args.name,
                "url": self.args.homepage,
                "description": "Read-only CI/CD reports for GitHub repositories: speed, failures, tests, DORA, AI use.",
                "callback_urls": [f"{base}/auth/callback"],
                "setup_url": f"{base}/",
                "setup_on_update": True,
                "redirect_url": f"http://127.0.0.1:{PORT}/created",
                "public": True,
                "request_oauth_on_install": False,
                "hook_attributes": {"url": f"{base}/", "active": False},
                "default_permissions": {"actions": "read", "contents": "read", "pull_requests": "read", "metadata": "read"},
                "default_events": [],
            }
        )

    def created(self, code: str, state: str) -> str:
        if state != self.state:
            return "This link does not belong to this setup run. Start again from the setup page."
        status, app = github(f"app-manifests/{urllib.parse.quote(code)}/conversions", method="POST")
        if status != 201 or not isinstance(app, dict):
            return f"GitHub did not hand over the app (HTTP {status}). The code is valid for one hour; start again."
        self.store(
            {"CIO_CLIENT_ID": app["client_id"], "CIO_APP_SLUG": app["slug"]},
            {"CIO_CLIENT_SECRET": ("cio-client-secret", app["client_secret"])},
        )
        self.app = {"slug": app["slug"], "html_url": app["html_url"], "owner": app["owner"]["login"]}
        return ""

    def token(self, value: str) -> str:
        value = value.strip()
        status, _ = github("user", token=value)
        if status != 200:
            return "GitHub did not accept that token. Copy it again, or make a new one."
        _, private = github("user/repos?visibility=private&per_page=1", token=value)
        if isinstance(private, list) and private:
            return (
                "That token can read private repositories, so it must not serve people who are not signed in. "
                "Make a fine-grained token with Repository access: Public repositories."
            )
        self.store({}, {"CIO_SERVER_TOKEN": ("cio-server-token", value)})
        self.token_ok = True
        return ""


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connect CI Observatory to GitHub</title>
<style>
:root{{color-scheme:dark}}body{{margin:0;background:#060a10;color:#e8f1f8;font:16px/1.6 system-ui,sans-serif}}
main{{max-width:720px;margin:0 auto;padding:40px 20px}}h1{{font-size:28px;margin:0 0 6px}}p{{color:#aec2d0}}
section{{border:1px solid rgba(160,200,225,.2);border-radius:12px;padding:18px 20px;margin-top:18px;background:#0c121b}}
h2{{font-size:19px;margin:0 0 6px}}.done{{color:#46d67a;font-weight:600}}.err{{color:#ff7373}}
button{{font:600 15px system-ui;background:#5fd3f3;color:#04131a;border:0;border-radius:999px;padding:10px 18px;
  cursor:pointer}}
input{{font:15px ui-monospace,monospace;width:100%;box-sizing:border-box;padding:10px;border-radius:8px;
  border:1px solid rgba(160,200,225,.3);background:#060a10;color:#e8f1f8;margin:8px 0}}
a{{color:#5fd3f3}}code{{font-size:14px}}ol{{color:#aec2d0;padding-left:20px}}
</style></head><body><main>
<h1>Connect CI Observatory to GitHub</h1>
<p>For <code>{public_url}</code>. Two steps; this page runs on your machine only.</p>
{message}
<section><h2>1. Create the GitHub App</h2>{step1}</section>
<section><h2>2. A token for public repositories</h2>{step2}</section>
{finish}
</main></body></html>"""


def page(setup: Setup, message: str = "") -> str:
    a = setup.args
    if setup.app:
        app = setup.app
        step1 = (
            f'<p class="done">Created: <a href="{html.escape(app["html_url"])}">{html.escape(app["slug"])}</a>, '
            f"owned by {html.escape(app['owner'])}. Its client ID and secret are stored.</p>"
        )
    else:
        target = (
            f"https://github.com/organizations/{urllib.parse.quote(a.org)}/settings/apps/new"
            if a.org
            else "https://github.com/settings/apps/new"
        )
        step1 = (
            "<p>GitHub opens with the app filled in: read-only Actions, Contents and Pull requests, and this "
            f"service's callback. Check the name (<code>{html.escape(a.name)}</code>; it must be unique on GitHub) "
            "and click <b>Create GitHub App</b>.</p>"
            f'<form method="post" action="{target}?state={setup.state}">'
            f'<input type="hidden" name="manifest" value="{html.escape(setup.manifest())}">'
            "<button>Create the GitHub App on github.com</button></form>"
        )
    if setup.token_ok:
        step2 = '<p class="done">Stored. People who are not signed in can now analyse public repositories.</p>'
    else:
        step2 = (
            '<ol><li>Open <a href="https://github.com/settings/personal-access-tokens/new" target="_blank" rel="noopener">'
            "a new fine-grained token</a>.</li><li>Name: <i>CI Observatory public reads</i>. Expiration: up to a year.</li>"
            "<li>Repository access: <b>Public repositories</b>. No permissions to add.</li>"
            "<li>Generate it, copy it, and paste it here.</li></ol>"
            '<form method="post" action="/token"><input name="token" type="password" placeholder="github_pat_..." '
            'autocomplete="off" required><button>Check and store the token</button></form>'
        )
    finish = ""
    if setup.app and setup.token_ok:
        finish = (
            f'<section><h2 class="done">All set</h2><p>Open <a href="{html.escape(a.public_url)}">'
            f"{html.escape(a.public_url)}</a>. "
            "You can close this page; the setup script has stopped.</p></section>"
        )
    note = f'<p class="err">{html.escape(message)}</p>' if message else ""
    return PAGE.format(public_url=html.escape(a.public_url), message=note, step1=step1, step2=step2, finish=finish)


def serve(setup: Setup) -> None:
    class Handler(BaseHTTPRequestHandler):
        def send_page(self, body: str, status: int = 200) -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/created":
                query = urllib.parse.parse_qs(url.query)
                try:
                    problem = setup.created(query.get("code", [""])[0], query.get("state", [""])[0])
                except RuntimeError as exc:
                    problem = str(exc)
                self.send_response(303)
                self.send_header("Location", "/?problem=" + urllib.parse.quote(problem) if problem else "/")
                self.end_headers()
                return
            problem = urllib.parse.parse_qs(url.query).get("problem", [""])[0]
            self.send_page(page(setup, problem))
            if setup.app and setup.token_ok:
                setup.done.set()

        def do_POST(self) -> None:
            if self.path != "/token" or self.headers.get("Origin") not in (f"http://127.0.0.1:{PORT}", None):
                self.send_page(page(setup, "Refused."), 403)
                return
            form = urllib.parse.parse_qs(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode())
            try:
                problem = setup.token(form.get("token", [""])[0])
            except RuntimeError as exc:
                problem = str(exc)
            self.send_response(303)
            self.send_header("Location", "/?problem=" + urllib.parse.quote(problem) if problem else "/")
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Open http://127.0.0.1:{PORT} in your browser and follow the two steps.", flush=True)
    setup.done.wait()
    print(f"Done: the GitHub App {setup.app['slug'] if setup.app else ''} and the public-data token are stored.", flush=True)
    server.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--public-url", required=True, help="the web app's address (CIO_PUBLIC_URL)")
    parser.add_argument("--name", default="CI Observatory", help="the GitHub App's name; must be unique on GitHub")
    parser.add_argument("--homepage", default="https://kirbac1.github.io/ci-observatory/")
    parser.add_argument("--org", help="create the app under this organisation instead of your own account")
    parser.add_argument("--cloud-run", metavar="SERVICE", help="store into Secret Manager and update this Cloud Run service")
    parser.add_argument("--region", default="europe-north1")
    parser.add_argument("--project", help="the Google Cloud project (with --cloud-run)")
    args = parser.parse_args()
    if args.cloud_run and not args.project:
        parser.error("--cloud-run needs --project")
    serve(Setup(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
