"""python -m ci_observatory.server: serve the web app with uvicorn. HOST and PORT choose where it listens."""

import logging
import os
import sys


def main() -> int:
    try:
        import uvicorn
    except ImportError:
        print(
            "error: the web server needs the 'server' extra, for example:\n"
            '  uv tool install "ci-observatory[server] @ git+https://github.com/kirbac1/ci-observatory"',
            file=sys.stderr,
        )
        return 2
    from ci_observatory.server.app import create_app
    from ci_observatory.server.config import Settings

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    if not settings.login_enabled:
        logging.warning("CIO_CLIENT_ID and CIO_CLIENT_SECRET are not set: signing in is off, public repositories only")
    if not settings.server_token:
        logging.warning("CIO_SERVER_TOKEN is not set: people must sign in before they can analyse anything")
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(create_app(settings), host=host, port=port, access_log=False, proxy_headers=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
