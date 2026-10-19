# Install

CI Observatory is one Python command, `ci-observatory`, with no dependencies outside the standard library. It reads
GitHub through the [GitHub CLI](https://cli.github.com), so whichever way you install it, you need `gh`, or a token in
`GH_TOKEN` (then it calls the API directly).

- [Requirements](#requirements)
- [The one-line installer](#the-one-line-installer)
- [Run once without installing](#run-once-without-installing)
- [Other ways to install](#other-ways-to-install)
- [Pin a version](#pin-a-version)
- [Upgrade](#upgrade)
- [Uninstall](#uninstall)
- [Check that it works](#check-that-it-works)
- [Troubleshooting the install](#troubleshooting-the-install)

## Requirements

| What | Why | How |
| --- | --- | --- |
| GitHub CLI `gh`, a recent version | requests go through it, so the tool never sees a token | `brew install gh`, or see <https://cli.github.com> for Linux and Windows |
| A `gh` login, or `GH_TOKEN` | to read your repositories; with `GH_TOKEN` and no `gh`, the tool calls the API itself | `gh auth login` ([private repositories](private-repositories.md) need the `repo` scope) |
| [uv](https://docs.astral.sh/uv/), or pipx with Python 3.11+ | to install and run the command | the installer below adds uv if it is missing |

Tested on macOS and Linux. Windows has not been tested; the `uvx` route below is the one to try there.

## The one-line installer

```sh
curl -LsSf https://kirbac1.github.io/ci-observatory/install.sh | sh
```

What it does, in order:

1. If `uv` is not on your `PATH`, it runs uv's official installer (`https://astral.sh/uv/install.sh`), which puts `uv`
   in `~/.local/bin`. uv downloads its own Python when needed, so your system Python is not touched.
2. It runs `uv tool install --force git+https://github.com/kirbac1/ci-observatory@main`. That builds the package in an
   isolated environment and links the `ci-observatory` command into uv's tool folder (`uv tool dir --bin`, usually
   `~/.local/bin`).
3. It checks that `gh` is installed and logged in, and that the tool folder is on your `PATH`, and tells you what to fix
   if not.

Nothing needs `sudo`, and nothing outside your user's folders changes. To read the script before running it:

```sh
curl -LsSf https://kirbac1.github.io/ci-observatory/install.sh -o install.sh
less install.sh
sh install.sh
```

The script is the same file as [`install.sh`](../install.sh) in this repository; the Pages workflow publishes it on every
push to `main`.

Two environment variables change what it installs:

| Variable | Default | Use |
| --- | --- | --- |
| `CI_OBSERVATORY_REF` | `main` | a tag, branch or commit to install, such as `v0.1.0` |
| `CI_OBSERVATORY_REPO` | `https://github.com/kirbac1/ci-observatory` | install from your fork instead |

```sh
curl -LsSf https://kirbac1.github.io/ci-observatory/install.sh | CI_OBSERVATORY_REF=v0.1.0 sh
```

If the installer says the tool folder is not on your `PATH`, run `uv tool update-shell` and open a new terminal.

## Run once without installing

`uvx` runs a command in a temporary environment and leaves nothing installed:

```sh
uvx --from git+https://github.com/kirbac1/ci-observatory ci-observatory --org your-org --open
```

The first run takes a few seconds to build; later runs reuse uv's cache. This is also the easiest way to try a
branch or a pull request:

```sh
uvx --from git+https://github.com/kirbac1/ci-observatory@some-branch ci-observatory --help
```

## Other ways to install

**uv, directly:**

```sh
uv tool install git+https://github.com/kirbac1/ci-observatory
```

**pipx:**

```sh
pipx install git+https://github.com/kirbac1/ci-observatory
```

**In GitHub Actions**, where pipx comes with the hosted runners, no install step is needed:

```sh
pipx run --spec git+https://github.com/kirbac1/ci-observatory ci-observatory --repo "$GITHUB_REPOSITORY"
```

See [GitHub Actions](github-actions.md) for complete workflows.

**From a clone**, to change the code:

```sh
git clone https://github.com/kirbac1/ci-observatory
cd ci-observatory
uv run ci-observatory --org your-org     # runs the code in this folder
```

Don't use `pip install` into a system Python. On macOS with Homebrew, and on recent Debian and Ubuntu, the system
Python is "externally managed" and refuses it; uv and pipx exist for exactly this.

## Pin a version

Releases are tagged `vX.Y.Z`. Add `@<tag>` to any of the sources above:

```sh
uv tool install git+https://github.com/kirbac1/ci-observatory@v0.1.0
uvx --from git+https://github.com/kirbac1/ci-observatory@v0.1.0 ci-observatory --version
pipx install git+https://github.com/kirbac1/ci-observatory@v0.1.0
```

For automation, pin a commit SHA instead of a tag: a SHA cannot be moved.

## Upgrade

Run the installer again; it reinstalls with `--force`. Or:

```sh
uv tool install --force git+https://github.com/kirbac1/ci-observatory
pipx install --force git+https://github.com/kirbac1/ci-observatory
```

With `uvx`, add `--refresh` to be sure it fetches the newest commit on `main` rather than a cached build.

## Uninstall

```sh
uv tool uninstall ci-observatory        # or: pipx uninstall ci-observatory
rm -rf ~/.cache/ci-observatory          # cached GitHub responses and job logs
```

Reports are wherever you wrote them (`./ci-observatory/` by default). Delete them like any other folder.

## Check that it works

```sh
ci-observatory --version
gh auth status
ci-observatory --repo cli/cli --days 7 --quick --open
```

The last command analyses a busy public repository in a minute or two and opens the report.

## Troubleshooting the install

| Message | Fix |
| --- | --- |
| `ci-observatory: command not found` | The tool folder is not on `PATH`. Run `uv tool update-shell` (or `pipx ensurepath`) and open a new terminal. |
| `error: curl or wget is needed to install uv` | Install curl, or install uv yourself from <https://docs.astral.sh/uv/>, then rerun. |
| `the GitHub CLI gh is not installed` | Install it from <https://cli.github.com>, then `gh auth login`. |
| `gh is not logged in` | `gh auth login`, or set `GH_TOKEN`. |
| `externally-managed-environment` | You used `pip install`. Use the installer, uv or pipx instead. |
| A TLS or proxy error while installing | uv, git and gh all honour `HTTPS_PROXY` and `SSL_CERT_FILE`; set them as your network requires. |
