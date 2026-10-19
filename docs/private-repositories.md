# Private repositories

Private repositories need nothing special in the command. CI Observatory reads GitHub only through the GitHub CLI, so
it can read exactly what your `gh` login, or the token you give `gh`, can read. This guide shows four ways to give it
that access, from the simplest to the most locked down, and what to do when GitHub says "Not Found".

- [What access it needs](#what-access-it-needs)
- [Option A: your own gh login](#option-a-your-own-gh-login)
- [Option B: a fine-grained personal access token](#option-b-a-fine-grained-personal-access-token)
- [Option C: GitHub Actions](#option-c-github-actions)
- [Option D: GitHub Enterprise Server](#option-d-github-enterprise-server)
- [Choosing which repositories](#choosing-which-repositories)
- [Keeping private data private](#keeping-private-data-private)
- [Troubleshooting](#troubleshooting)

## What access it needs

Read access, and nothing more. Every request is a GET.

| GitHub permission | Level | Used for |
| --- | --- | --- |
| Actions | read | workflow runs, jobs, steps and job logs |
| Contents | read | workflow files, commits, releases, agent instruction files such as `AGENTS.md` |
| Pull requests | read | pull requests, their commits and reviews |
| Metadata | read | repository lists (always granted) |

With the GitHub CLI installed, the tool never reads, prints or stores a token: it runs `gh`, and `gh` attaches the
credentials. Without `gh`, it reads `GH_TOKEN` from the environment and sends it to the GitHub API only.

## Option A: your own gh login

The quickest route when you can already see the repositories on github.com.

1. **Check your login.**

   ```sh
   gh auth status
   ```

   Look at the `Token scopes` line. You need `repo`, and `read:org` for organisations (a default `gh auth login`
   asks for both).

2. **Add missing scopes.** This opens the browser once:

   ```sh
   gh auth refresh -h github.com -s repo,read:org
   ```

3. **Authorise single sign-on, if your organisation uses it.** Organisations with SAML SSO hide their private
   repositories from any app that has not been authorised for them. Open <https://github.com/settings/applications>,
   choose **GitHub CLI** under *Authorized OAuth Apps*, and click **Grant** (or **Authorize**) next to the organisation.
   If you skip this, the private repositories return "Not Found" or "Resource protected by organization SAML
   enforcement".

4. **Run it.**

   ```sh
   ci-observatory --org my-org --visibility private --open    # the busiest private repositories
   ci-observatory --repo my-org/payments --open               # one repository
   ```

   Before collecting, the tool checks that every `--repo` is readable and lists which ones are private.

Your own account works the same way: `ci-observatory --org my-login` includes your private repositories when your
login has the `repo` scope.

## Option B: a fine-grained personal access token

Use this when you want least privilege, an expiry date, or a token that only sees a few repositories, for example on a
shared machine or in a scheduled job outside GitHub Actions.

1. On GitHub, open **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new
   token**.
2. **Resource owner:** the organisation (or your account) that owns the repositories.
3. **Expiration:** as short as is practical.
4. **Repository access:** *Only select repositories* and pick them, or *All repositories*.
5. **Repository permissions:** set **Actions**, **Contents** and **Pull requests** to **Read-only**. Metadata is set to
   read-only automatically. Leave everything else at *No access*. With `--org`, the tool sees the repositories the
   token was given, and no others.
6. Click **Generate token** and copy it.
7. If the organisation requires approval for fine-grained tokens, the token stays pending until an owner approves it.
   If the organisation uses SSO, click **Configure SSO** next to the token and authorise it.
8. Hand the token to `gh` through the environment:

   ```sh
   export GH_TOKEN=github_pat_...
   ci-observatory --org my-org
   ```

   `gh` uses `GH_TOKEN` instead of your stored login. The tool skips its `gh auth status` check when `GH_TOKEN` or
   `GITHUB_TOKEN` is set.

A classic token with the `repo` and `read:org` scopes also works, but it can write to every repository you can, which is
far more than this tool needs.

## Option C: GitHub Actions

Inside a workflow, the job's own `GITHUB_TOKEN` can read the repository it runs in, private or not, given these
permissions:

```yaml
permissions:
  actions: read
  contents: read
  pull-requests: read
```

To cover several private repositories from one workflow, use a GitHub App installed on them. Both setups are described
step by step in [GitHub Actions](github-actions.md).

## Option D: GitHub Enterprise Server

Log in to your instance, then tell `gh` which host to use:

```sh
gh auth login --hostname github.example.com
GH_HOST=github.example.com ci-observatory --org my-org
```

With a token instead of a login, use `GH_ENTERPRISE_TOKEN` together with `GH_HOST`. Job logs, releases and pull
requests use the same API on Enterprise Server, so every section works if Actions is enabled on the instance.

## Choosing which repositories

| You want | Flags |
| --- | --- |
| Only private repositories of an organisation | `--org my-org --visibility private` |
| Only public ones | `--org my-org --visibility public` |
| More than the 12 busiest | `--top 30` |
| Quiet repositories too | `--min-runs 1` (the default skips repositories with fewer than 10 runs in the window) |
| An exact list | `--repo my-org/a --repo my-org/b` (`--top` and `--min-runs` don't apply) |
| Several organisations | `--org one --org two` |

`--org` ranks repositories by the number of workflow runs in the window and keeps the busiest.

## Keeping private data private

Everything is written to your machine, and nowhere else:

- `./ci-observatory/index.html` and `./ci-observatory/data/*.json` (or the `--out` folder);
- cached API responses and gzipped job logs in `~/.cache/ci-observatory/OWNER/NAME/`.

The report contains repository and workflow names, job and step names, cleaned log excerpts (names, emails, tokens, IP
addresses and commit subjects are removed), and the titles of pull requests that name an AI tool. Treat it like the
repositories it describes:

- Don't publish a report on private repositories to a public place, such as a public GitHub Pages site or a public
  gist.
- In GitHub Actions, keep the report as a workflow artifact with a short retention, which only people with read access
  to the repository can download.
- Delete `~/.cache/ci-observatory/` when you are done, if the machine is shared.

The tool does not touch your `.gitignore`. If you run it inside a repository, add `ci-observatory/` there so a report
is never committed by accident.

## Troubleshooting

| What you see | Cause | Fix |
| --- | --- | --- |
| `cannot read OWNER/NAME` | The name is wrong, or your login cannot see it | Check the name on github.com, then follow Option A steps 2 and 3. |
| `HTTP 404: Not Found` on a repository you can open in the browser | Missing `repo` scope, or SSO not authorised for the GitHub CLI or the token | `gh auth refresh -s repo,read:org`, then authorise SSO (Option A step 3 or Option B step 7). |
| `Resource protected by organization SAML enforcement` | SSO not authorised | Authorise the GitHub CLI app or the token for the organisation. |
| `--org` finds only public repositories | The login or token cannot see the private ones: a missing scope, SSO not authorised, or a token limited to other repositories | Check `gh auth status`, authorise SSO, or widen the token's repository access. |
| `Although you appear to have the correct authorization credentials, the ... organization has enabled OAuth App access restrictions` | The organisation blocks OAuth apps it has not approved | Ask an owner to approve the GitHub CLI, or use a fine-grained token (Option B). |
| A fine-grained token returns 403 | The token is pending approval, expired, or lacks a permission | Check its status under Settings → Developer settings, and its permissions against the table above. |
| `HTTP 410` or `404` for a log | The log expired (90 days by default; private repositories can set it lower) or was deleted | Nothing to fix; the job is skipped and the report says so. |
| Rate limited | 5,000 requests an hour per user or token | Wait for the reset, or use `--quick` or fewer repositories. Cached data is reused. |
