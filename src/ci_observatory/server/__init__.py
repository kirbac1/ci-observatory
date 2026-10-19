"""CI Observatory as a web app: paste a repository, watch the analysis run, read the report.

Public repositories work without signing in, read with the server's own token. Signing in with GitHub (a GitHub App,
read-only) lets people analyse private repositories they can read. The token stays on the server for the session and
is revoked on sign-out; private reports are shown only to the person who asked for them and deleted soon after.

Run it with `python -m ci_observatory.server`; configuration is read from the environment (see config.py).
"""
