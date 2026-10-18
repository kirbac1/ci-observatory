#!/bin/sh
# Install the ci-observatory command.
#
#   curl -LsSf https://kirbac1.github.io/ci-observatory/install.sh | sh
#
# It installs uv (a fast Python package manager, which also fetches a suitable Python) if you don't have it, then
# installs ci-observatory as an isolated command with `uv tool install`. Nothing needs sudo, and nothing outside your
# user's tool folders is touched. Uninstall with: uv tool uninstall ci-observatory
#
# Pin a version:    curl -LsSf https://kirbac1.github.io/ci-observatory/install.sh | CI_OBSERVATORY_REF=v0.1.0 sh
set -eu

REPO="${CI_OBSERVATORY_REPO:-https://github.com/kirbac1/ci-observatory}"
REF="${CI_OBSERVATORY_REF:-main}"

say() { printf '%s\n' "$*"; }
note() { printf '  ! %s\n' "$*"; }

if ! command -v uv >/dev/null 2>&1; then
  say "Installing uv, the Python package manager ci-observatory runs on ..."
  if command -v curl >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- https://astral.sh/uv/install.sh | sh
  else
    say "error: curl or wget is needed to install uv. See https://docs.astral.sh/uv/ to install it yourself." >&2
    exit 1
  fi
  for dir in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    if [ -x "$dir/uv" ]; then PATH="$dir:$PATH"; fi
  done
  export PATH
fi

say "Installing ci-observatory from $REPO ($REF) ..."
uv tool install --force "git+$REPO@$REF"

say ""
problems=0
if ! command -v gh >/dev/null 2>&1; then
  note "The GitHub CLI (gh) is missing. ci-observatory reads GitHub through it. Install it from https://cli.github.com"
  problems=1
elif [ -z "${GH_TOKEN:-}${GITHUB_TOKEN:-}" ] && ! gh auth status >/dev/null 2>&1; then
  note "gh is not logged in yet. Run: gh auth login"
  problems=1
fi
bin_dir=$(uv tool dir --bin 2>/dev/null || printf '%s' "$HOME/.local/bin")
case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *) note "$bin_dir is not on your PATH yet. Run: uv tool update-shell   (then open a new terminal)"; problems=1 ;;
esac

if [ "$problems" -eq 0 ]; then say "Done. Try it:"; else say "Installed. Fix the notes above, then try it:"; fi
say "  ci-observatory --org <your-org> --open        # the busiest repositories of an organisation"
say "  ci-observatory --repo <owner>/<name> --open   # one repository, public or private"
say "  ci-observatory --help"
