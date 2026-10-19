# How it works

This page explains what CI Observatory reads, how every number in the report is made, and where those numbers stop
being trustworthy. It is written for people who want to check the method, extend the tool, or use its data elsewhere.

- [The pipeline](#the-pipeline)
- [What it reads](#what-it-reads)
- [Output files](#output-files)
- [CI numbers](#ci-numbers)
- [Job logs](#job-logs)
- [Test audit](#test-audit)
- [Delivery (DORA)](#delivery-dora)
- [AI signals](#ai-signals)
- [Solution box](#solution-box)
- [Privacy](#privacy)
- [Caching, budgets and rate limits](#caching-budgets-and-rate-limits)
- [Limits](#limits)
- [Code layout](#code-layout)

## The pipeline

```
                ┌──► 1. actions ──► runs, jobs, steps, workflow hygiene ────────┐
gh api (GET) ───┼──► 2. logs    ──► decoded job logs, failure causes,          ├──► 4. report ──► index.html
                │                   test redundancy audit                      │
                └──► 3. signals ──► AI tools and models, AI in CI, DORA ───────┘
```

`ci-observatory` runs the four steps in order and writes everything under `--out` (default `./ci-observatory`). Each
step is a module you can also run on its own, with its own `--help`:

```sh
python -m ci_observatory.actions --repos my-org/api --days 30 --out data/metrics.json --dash-dir data
python -m ci_observatory.logs    --runs data/runs.json --out data/logs.json --audit-out data/tests.json
python -m ci_observatory.signals --repos my-org/api --days 30 --runs data/runs.json --out data/signals.json
python -m ci_observatory.report  --data-dir data --out report.html
```

The report step makes no network calls, so a report can be rebuilt from saved data at any time, for example after
changing the page.

## What it reads

Every request is a GET against the GitHub REST API (version `2022-11-28`). Nothing is written. Requests go through
`gh api --method GET` when the GitHub CLI is installed, so the tool never sees a token. Where `gh` is missing and
`GH_TOKEN` is set (a container, the web app), they go directly over HTTPS with gzip; the token is sent to the API host
only, and redirects to log storage are followed without it. `CI_OBSERVATORY_TRANSPORT=gh` or `=http` forces either.

| Step | Endpoints | Notes |
| --- | --- | --- |
| discovery (`--org`) | `orgs/{org}`, `orgs/{org}/repos`, `users/{user}/repos`, `user/repos` | ranks repositories by runs in the window and keeps the `--top` busiest with at least `--min-runs` |
| 1. actions | `repos/{r}/actions/runs`, `…/runs/{id}/jobs`, `…/actions/workflows`, `contents/{workflow file}`, `contents/.github/dependabot.yml`, `pulls`, `releases` | the newest 1,000 runs per repository in the window; jobs for a sample of runs |
| 2. logs | `…/actions/runs/{id}/jobs`, `…/actions/jobs/{id}/logs` | the latest failed and successful jobs per repository, then every job of one recent pull request commit |
| 3. signals | `repos/{r}`, `contents/`, `contents/.github`, `…/workflows/{id}/runs`, `commits`, `pulls`, `pulls/{n}/commits`, `pulls/{n}/reviews`, `releases` | instruction files, AI workflows and their runs, pull requests with commits and reviews, default-branch commits |

## Output files

```
ci-observatory/
├── index.html              the report, with all data, styles and scripts inlined
└── data/
    ├── about.json          window, organisations, generation time, limits used
    ├── runs.json           one row per workflow run: repository, workflow, event, result, start, duration, actor kind
    ├── repos.json          one row per repository: run counts, pass rate, durations, workflow hygiene
    ├── jobs.json, steps.json, workflows.json, pulls.json, releases.json, pr_commits.json
    ├── metrics.json        the CI summary from step 1
    ├── logs.json           decoded logs of the sampled jobs (no raw logs): steps, activity, failure, cleaned lines
    ├── tests.json          the test audit: per repository, tests, repeats and their causes
    └── signals.json        AI and DORA signals per repository, plus totals and agent comparisons
```

All files are plain JSON. `runs.json`, `repos.json` and `signals.json` are the most useful outside the report.

## CI numbers

- **Runs.** Workflow runs created in the window. Very busy repositories are cut to their newest 1,000; the method note
  in the report says when that happened.
- **Pass rate.** Successful runs divided by runs that finished as success or failure. Cancelled, skipped and
  still-running runs are left out, so a cancelled duplicate does not count against a repository.
- **Duration.** From the run's start to its last update, capped at 6 hours. Re-running a job moves a run's end days
  later; no hosted job can run longer than 6 hours, so the cap removes that distortion. Runs that never executed
  (waiting for approval, or skipped) count as 0.
- **Typical and slow.** The median and the 90th percentile of run durations.
- **Who started a run.** People, bots (Dependabot, Renovate, GitHub Actions, pre-commit.ci and similar), AI agents
  (such as the Copilot coding agent), or schedules, from the run's actor and event.
- **Workflow hygiene** is read from the workflow files: `permissions` blocks, actions pinned to a 40-character SHA,
  and `timeout-minutes` on jobs.

## Job logs

Logs are downloaded with timestamps, decoded and reduced to what the report needs; raw logs stay in the local cache.

- **Steps.** GitHub truncates step start and end times to the second, so lines are assigned to steps by the markers in
  the log itself (`##[group]Run …`, `Post job cleanup.`, `Cleaning up orphan processes`), not by time.
- **Kinds of work.** Each step is classified as setup, dependencies, containers, build, test, lint, security or other,
  from its name, the commands it runs and patterns in its output.
- **Quiet stretches.** Gaps of 20 seconds or more between log lines, usually a compile, a download or a test runner
  that prints only at the end.
- **Failure cause.** Only what the failed step printed is read, not the inputs it echoed. The first matching class
  wins, in this order:

  | Class | Recognised by |
  | --- | --- |
  | Timed out | the runner's timeout and cancellation messages |
  | Broken workflow setup | invalid workflow files, missing inputs or secrets, unknown actions |
  | Tests failed | failing test lines from pytest, Maven, Gradle, Go and JavaScript runners |
  | Lint or formatting | linter and formatter findings, pre-commit hooks that failed |
  | Network or rate limit | DNS and TLS errors, HTTP 5xx, `toomanyrequests`, download failures |
  | Dependency problem | resolver and lock-file errors, Dependabot jobs |
  | Another job failed first | jobs that only report the result of other jobs |
  | PR title or changelog check | process checks named for titles, labels, changelogs or commit messages |
  | by step | otherwise, the kind of the failed step (test, lint, dependencies) |
  | Build error | compiler and build-tool errors |
  | Other | anything else |

  The *signature* is the most telling line: the failing test or lint finding, an annotated error, an error-like line,
  or the last thing the step printed.
- **Facts** per job: tests run and failed (from the runners' summaries), cache hits and misses, files downloaded,
  warnings, deprecation notices, the token's permissions as GitHub printed them, and the runner image.

## Test audit

For each repository, the tool picks the newest pull request commit on which no workflow failed and at least two
succeeded (falling back to the newest with any success), reads every job of every run on that commit, and extracts the
tests each job ran. A clean commit is chosen so that repeats are not confused with retries of failures.

- **What counts as a test.** The most precise unit each runner prints: a pytest file (or test id when verbose), a
  Maven test class with its test count, a Gradle test, a Go test or package, a JavaScript test file. Test ids that
  GitHub partly masked as secrets (`***`) are left out, because two different tests can look alike once masked.
- **Matching.** A test is the same test when the same unit appears in two jobs. Job names are normalised so that
  `test (3.12)` and `test (3.13)` are recognised as matrix legs of one job: parenthesised suffixes and standalone
  version numbers are stripped.
- **Buckets.** Every repeat goes in exactly one bucket, checked in this order:

  | Bucket | The same test also ran … | Verdict |
  | --- | --- | --- |
  | in a security scan | inside a CodeQL or similar scan job | waste: the scan only needs to build |
  | double trigger | in the same workflow started by both `push` and `pull_request` | waste |
  | another workflow | in a different workflow | waste |
  | same workflow | in another job of the same workflow that is not a matrix leg | waste |
  | matrix | in another leg of a version matrix (Python, Java, Node, OS) | deliberate coverage, but a cost |
  | within a job | twice inside one job | waste |

- **Extra time for versions** is, per matrix job, the duration of every leg after the longest one: the compute that
  testing every version on every pull request adds.

Repositories whose logs don't name their tests (for example a runner without verbose output) are listed as blind
spots and excluded from the totals.

## Delivery (DORA)

The four [DORA](https://dora.dev) measures, estimated from GitHub data. They are proxies: GitHub does not know when
code reached production or when users were affected.

- **Deployments.** Stable releases (not drafts or pre-releases) when a repository has at least two in the window.
  Otherwise, successful runs on the default branch of workflows named like a deployment (`deploy`, `publish`,
  `release`, `pages`, `docs`, `helm`, `chart`, `goreleaser`). Otherwise there is no deployment data.
- **Deployment frequency.** Deployments per week over the window.
- **Lead time for changes.** For each merged pull request not opened by a bot: from opening the pull request to the
  first deployment at or after its merge. The median is shown; the 90th percentile and the share of merges not yet
  deployed are in the data.
- **Change failure rate.** With releases: the share of releases followed within 72 hours by a patch release of the same
  major and minor version, or within 7 days by a revert commit. With deploy runs: the share of deploy runs that failed.
- **Time to restore.** On the default branch, per workflow, from a failed `push` run to the next successful run of the
  same workflow. The median is shown. The share of the window when the main branch was red is also computed.
- **Bands** follow the DORA research's thresholds:

  | Measure | Elite | High | Medium | Low |
  | --- | --- | --- | --- | --- |
  | Deployment frequency | ≥ 7 a week | ≥ 1 a week | ≥ 1 a month | less |
  | Lead time | < 1 day | < 1 week | < 1 month | longer |
  | Change failure rate | ≤ 5% | ≤ 10% | ≤ 15% | higher |
  | Time to restore | < 1 hour | < 1 day | < 1 week | longer |

## AI signals

AI use is counted only where it leaves a trace. Real use is higher; these numbers are a lower bound.

- **AI accounts.** Pull requests, commits, reviews and runs by known AI accounts: the Copilot coding agent and Copilot
  reviewer, CodeRabbit, Qodo, Greptile, Ellipsis, Korbit, cubic, Bito and others. Dependabot, Renovate and similar are
  automation, not AI.
- **Commit trailers.** `Assisted-by:`, `Generated-by:`, `AI-assisted-by:`, `Made-with:` and similar always mean AI.
  `Co-authored-by:` also names people, so it counts only when it is unmistakably an AI: a vendor address such as
  `noreply@anthropic.com`, or an agent's own name. A trailer that names a model but no tool is recorded as *Tool not
  named*.
- **Pull request descriptions.** Lines such as "Generated with Claude Code" or "Generated by Copilot".
- **Models** are recognised by name in trailers and descriptions: Claude (Opus, Sonnet, Haiku, Fable) with versions,
  GPT, Gemini, Grok and Cursor Composer.
- **AI workflows** are workflow files that use an AI action or key (`anthropics/claude-code-action`,
  `openai/codex-action`, `run-gemini-cli`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, Copilot review and agent setup), or
  that enforce AI disclosure.
- **Instruction files** are files AI agents read: `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `.github/copilot-instructions.md`,
  path instructions, prompt files, Cursor, Windsurf and Cline rules, `.mcp.json`, `llms.txt` and dev containers.
- **CI time involving AI** is the run time of AI review and agent workflows, runs started by AI accounts, and runs on
  pull requests that name AI.
- **Problem rate.** A pull request had a problem if its first CI run failed, a reviewer requested changes, or it was
  reverted or followed by a fix soon after. Rates come with Wilson 95% intervals. A tool is called better or worse than
  the no-AI baseline only when the intervals don't overlap, and only for groups of at least five pull requests.

## Solution box

The Solution box is a set of 19 rules in the page. Each rule looks at the data and, if it finds the problem, makes a
card with the affected repositories, the evidence in numbers, the expected gain and the fix.

| Phase | Rules |
| --- | --- |
| 1. Remove waste | tests inside security scans · CI running twice per push · every version tested on every pull request |
| 2. Fix what breaks | deploys that always fail · a red main branch · write-all CI tokens · unpinned actions · outdated actions · flaky downloads and rate limits · repositories where runs usually fail · tests that never run |
| 3. Speed up | dependencies downloaded every time · pipelines over ten minutes |
| 4. Prevent | test jobs that don't say which tests ran · jobs without a time limit · process checks that only say "red" · merged work waiting for a release · no instructions for AI agents · AI-assisted commits not marked |

Cards are ranked by severity (Critical 9, High 6, Medium 3, Low 1) times confidence, divided by effort (quick fix 1,
some work 2). The test rules use the Test audit's buckets, so a deliberate matrix is reported as a cost to weigh, not as
waste.

## Privacy

- **People are not named.** Authors and reviewers are recorded only as human, bot or AI; only bots and AI accounts keep
  their names. Pull
  request titles are kept only for pull requests that name AI, so the AI section can show examples.
- **Log lines are cleaned** before they are stored. Lines that could name a person, such as author lines, are
  dropped. Commit subjects are cut from checkout and merge lines. Emails, IP addresses, and GitHub, Slack and AWS
  tokens are replaced with placeholders, URL query strings are trimmed, and runner paths such as
  `/home/runner/work/…` are shortened. Lines GitHub masked as `***` stay masked.
- **Raw logs** stay in the local cache and never enter the report or `data/`.

## Caching, budgets and rate limits

- **Responses.** JSON responses are cached by `gh` for 6 hours (`gh api --cache 6h`), so rerunning the same command
  soon after is fast and cheap.
- **Logs.** Job logs are cached gzipped in `~/.cache/ci-observatory/OWNER/NAME/` (or under `$XDG_CACHE_HOME`) and
  reused on every later run. Logs of finished jobs never change, so the cache doesn't expire. Delete it whenever you
  like.
- **Budgets.** Each step stops cleanly after `--max-calls` requests (default 4,000), and the logs step after a download
  size limit. A step that stops early still writes what it has, the report notes the gap, and the command exits with
  code 1.
- **Rate limits.** A logged-in user or token gets 5,000 requests an hour. When GitHub says the limit is reached, the
  step stops as if its budget ran out. `--quick` uses smaller samples and roughly halves the calls.
- **Concurrency.** Up to four requests run at a time.

## Limits

- **Only what GitHub shows.** No production incidents, no deployments outside GitHub, and no AI use that leaves no
  trace.
- **Samples, not census.** Logs are the most recent failed and passed jobs, and the test audit reads one pull request
  per repository. Numbers from a handful of jobs are hints, not verdicts.
- **Proxies.** DORA measures are estimates from releases and CI; a repository that ships continuously without
  releases or deploy workflows shows no delivery data.
- **Log retention.** GitHub keeps logs for 90 days by default, and private repositories can set it lower; older jobs
  are skipped.
- **GitHub Actions only.** Other CI systems that report checks to GitHub are not read.

## Code layout

```
src/ci_observatory/
├── cli.py        the ci-observatory command: checks, discovery hints, runs the four steps
├── gh.py         a small wrapper around `gh api` with caching, a call budget and parallel requests
├── actions.py    step 1: runs, jobs, steps, workflow files and hygiene
├── logs.py       step 2: log download and decoding, failure causes, test extraction and the redundancy audit
├── signals.py    step 3: AI accounts, trailers and models, AI workflows, pull requests, DORA
├── report.py     step 4: inlines the page, styles, scripts and data into one HTML file
└── web/
    ├── page.html         the page skeleton
    ├── observatory.css   the styles
    ├── obs-core.js       shared helpers: formatting, scales, tooltips, tables, state
    ├── obs-fleet.js      Overview
    ├── obs-deep.js       Delivery, Inside a job, Health check, Test audit
    ├── obs-ai.js         AI impact, Solution box, How it's measured
    └── obs-main.js       start-up, menu, focus filter
```

The page uses plain SVG and no libraries. Data is inlined as JSON with every `<` escaped, so no string in the data can
close the script element.
