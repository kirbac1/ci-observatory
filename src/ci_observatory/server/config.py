"""Server settings, all from environment variables so the same image runs locally and in the cloud."""

import os
import tempfile
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Settings:
    #: Where people reach the app. Used for the sign-in callback and to check where requests come from.
    public_url: str = "http://localhost:8000"
    #: The GitHub App's client ID and secret. Without them, signing in is switched off.
    client_id: str = ""
    client_secret: str = ""
    #: The app's URL name (github.com/apps/<slug>), for the "install on your organisation" link.
    app_slug: str = ""
    #: Only for a classic OAuth App, which asks for scopes (for example "repo"). A GitHub App takes its permissions
    #: from its settings, so leave this empty.
    oauth_scope: str = ""
    #: Reads public repositories for people who are not signed in. Use a token that can read public data only.
    server_token: str = ""
    data_dir: Path = field(default_factory=lambda: Path(tempfile.gettempdir()) / "ci-observatory-server")
    #: Analyses running at once, and waiting in line.
    max_jobs: int = 2
    queue_limit: int = 20
    job_timeout: int = 900
    #: How long a public report is reused, and how long a private one is kept.
    public_cache_hours: float = 6.0
    private_keep_minutes: int = 60
    #: Analyses one visitor who is not signed in may start per hour, and all of them together.
    anon_per_hour: int = 6
    anon_total_per_hour: int = 60
    #: Analyses one signed-in person may start per hour.
    user_per_hour: int = 20
    #: Below this many remaining calls on the server token, only signed-in people can start analyses.
    min_server_quota: int = 600
    #: A whole owner costs far more calls than one repository, so it needs this many left when not signed in.
    min_owner_quota: int = 2500
    #: GitHub's web and API addresses; changed only by tests.
    github_url: str = "https://github.com"
    api_url: str = "https://api.github.com"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Settings":
        """CIO_PUBLIC_URL, CIO_CLIENT_ID, CIO_CLIENT_SECRET, CIO_SERVER_TOKEN, ... (the field name, upper case)."""
        source = os.environ if env is None else env
        values: dict[str, Any] = {}
        for f in fields(cls):
            raw = source.get(f"CIO_{f.name.upper()}")
            if raw is None or raw == "":
                continue
            kind = type(getattr(cls(), f.name))
            values[f.name] = Path(raw) if kind is Path else kind(raw)
        return cls(**values).checked()

    def checked(self) -> "Settings":
        if not self.public_url.startswith(("http://", "https://")):
            raise ValueError("CIO_PUBLIC_URL must start with http:// or https://")
        return self

    @property
    def login_enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def origin(self) -> str:
        scheme, rest = self.public_url.split("://", 1)
        return f"{scheme}://{rest.split('/', 1)[0]}"

    @property
    def callback_url(self) -> str:
        return self.public_url.rstrip("/") + "/auth/callback"

    @property
    def install_url(self) -> str | None:
        return f"{self.github_url}/apps/{self.app_slug}/installations/new" if self.app_slug else None
