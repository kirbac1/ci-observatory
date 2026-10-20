# Reading the report

The report has seven sections and a method note, one at a time, picked from a sidebar. Each chart is titled with the
question it answers, and
the sentence under it gives the answer for your data. This guide explains what each part shows, how to read it, and
what to do about it. The [live demo](https://kirbac1.github.io/ci-observatory/demo/) is a good companion.

- [Moving around](#moving-around)
- [1. Overview](#1-overview)
- [2. Delivery (DORA)](#2-delivery-dora)
- [3. Health check](#3-health-check)
- [4. Inside a job](#4-inside-a-job)
- [5. Test audit](#5-test-audit)
- [6. AI impact](#6-ai-impact)
- [7. Solution box](#7-solution-box)
- [How it's measured](#how-its-measured)

## Moving around

- **Sections.** The sidebar lists the sections in four groups (the big picture, the causes, AI, and what to do), each
  with a one-line description and its headline number, such as the pass rate or how many fixes were found. Click one
  to open it; *Previous* and *Next* at the bottom walk through them in order. The address changes with the section
  (`index.html#tests`), so the back button works and a link opens the section it names. On a phone the sidebar folds
  into a menu button at the top.
- **One long page.** *Show everything on one page*, under the sections, puts them all on one scrolling page again,
  handy for searching with Ctrl+F or printing. The browser remembers the choice.
- **Focus on one repository.** Pick it in the *Repositories* list at the top of the sidebar, or click a repository
  anywhere (a bubble, a table row, a bar): every section narrows to it. Pick *All repositories*, click *Show all* or
  press **Esc** to go back.
- **Tables.** Click a column heading to sort a table. The bubble chart has a *Show as table* button for the same
  numbers as rows.
- **Tooltips.** Hover or tap any bar, bubble or segment for the exact numbers.
- **Offline.** The file has everything inside it. Only the two web fonts come from the network, and the page falls
  back to system fonts without them.

## 1. Overview

How CI is doing across all repositories.

**Headline numbers.** Runs in the window, the share that passed, a typical run's duration (the median: half of all
runs are faster), the duration 1 in 10 runs exceeds (the 90th percentile: the long waits people notice), and the
number of repositories. Pass rate counts only runs that finished as success or failure; cancelled and skipped runs are
left out.

**Which repositories are fast and reliable?** One bubble per repository. Up is a higher pass rate; left is a shorter
typical run; a bigger bubble means more runs. The time axis is logarithmic so a 1-minute and a 30-minute pipeline both
fit. The tinted corners mark the ends that matter:

| Where a bubble sits | What it means | What to do |
| --- | --- | --- |
| Top left (green) | fast and reliable | leave it alone |
| Bottom right (red) | slow and often red | first candidate for the Solution box's speed and reliability fixes |
| Bottom left | fast but often red | usually flaky tests or a process check failing on purpose; see the Health check |
| Top right | reliable but slow | caching, splitting jobs, or running less on pull requests |

**When do runs start?** Runs per hour of the day, in UTC, Helsinki time or your browser's time zone. The shaded band
is 09:00–17:00. Peaks at night or on the hour are usually scheduled workflows; check whether they need to run that
often.

**How many runs pass each day?** Passed and failed runs per day, stacked. Weekends are shaded. A day with a sudden red
block is usually one broken change or an outage of an external service; click into *Inside a job* to see which.

## 2. Delivery (DORA)

The four [DORA](https://dora.dev) measures of software delivery, each rated Low, Medium, High or Elite. They are
estimates from GitHub data (releases, pull requests and CI), not from production; see
[How it works](how-it-works.md#delivery-dora) for the exact definitions.

| Tile | What it measures | Elite | High | Medium | Low |
| --- | --- | --- | --- | --- | --- |
| How often they ship | releases (or successful deploy runs) per week | 7 or more | 1 to 7 | 1 a month to 1 a week | less |
| From pull request to release | median time from opening a pull request to the first release after its merge | under a day | under a week | under a month | longer |
| Releases that needed a fix | share of releases followed by a patch release within 72 hours or a revert within 7 days | 5% or less | 10% or less | 15% or less | more |
| Time to fix a broken main branch | median time from a failing run on the default branch to the next passing run of the same workflow | under an hour | under a day | under a week | longer |

The tiles show the typical repository (the median). The sentence under them names the strongest and weakest measure.

**How does each repository do?** The same four measures per repository, with their ratings. A dash means there was no
data: no releases in the window, or the main branch was never red.

**When did each repository ship?** A timeline with a mark for every release or deploy. Long gaps followed by bursts
usually mean releases are manual; regular marks mean release automation.

What to do: a low *how often* with a high *pull request to release* time usually means merged work waits for a manual
release. The Solution box suggests release automation when it sees that pattern.

## 3. Health check

**Why do jobs fail?** Failed jobs from the sample, grouped by cause. The colour says where the problem is: in the code
(tests, lint, build), or around it (timeouts, network and rate limits, broken workflow setup, dependency resolution,
PR title or changelog checks, or another job failing first). The sentence under the chart says how many
failures had nothing to do with the code, which is the share a better setup could prevent. *Show an example of each*
opens one real failure per cause.

**Where does job time go?** Per repository, the share of job time spent on each kind of work, from successful jobs.
A large setup or dependencies share means caching would help; a large tests share is where the Test audit helps.

**Are the safety basics in place?** One row per repository:

| Column | Good | Why it matters |
| --- | --- | --- |
| CI token rights | the job's token is read-only | a compromised step with a write token can push code or publish packages |
| Actions pinned | third-party actions pinned to a full commit SHA | a tag can be moved to malicious code; a SHA cannot |
| Time limits set | workflows set `timeout-minutes` | without one, a stuck job runs for 6 hours |
| Cache works | cache hits rather than misses | a cache that never hits costs time to save and restore |
| Outdated actions | no "deprecated" warnings from GitHub | deprecated actions stop working when GitHub removes the old runtime |

## 4. Inside a job

Pick any sampled job on the left (filter by *Failed* or *Passed*, or search by name). The tool reads the latest failed
and passed jobs of each repository.

- **The answer line** says how long the job took, what kind of work took most of it, and how long it printed nothing.
- **What went wrong** (failed jobs only) gives the failure class and the most telling line of the failed step, such as
  the failing test or the first compiler error.
- **Where the time went** splits the job into kinds of work: setup, dependencies, containers, build, tests, lint,
  security scans and other. The kind comes from step names, the commands they ran and patterns in the log.
- **Steps, in order** is a timeline. Hatched stretches are 20 seconds or more with no output, typically a compile, a
  download or a test run that prints only at the end. A red bar is the step that failed.
- **Facts** lists tests run and failed, cache hits and misses, files downloaded, warnings, what the job's token could
  write to, and the runner image.
- **The job's log** shows the important lines (commands, errors, warnings, test results and quiet stretches), only
  problems, or everything. **Play** replays the log at speed with a moving playhead.

Log lines are cleaned before they are stored: names, emails, tokens, IP addresses and commit subjects are removed, and
runner paths are shortened.

## 5. Test audit

For one recent pull request per repository, the tool reads every job of that commit, extracts every test it ran
(pytest, Maven Surefire, Gradle, Go and JavaScript runners), and matches them across jobs.

**Headline numbers.** Test runs on those commits, distinct tests, repeats with no benefit (the same test run again
with nothing new learned), and the extra time spent testing every language version.

**How often does each test run?** Per repository, test runs split into *each test once* (grey), *repeated for another
version* (blue: matrix legs on other Python, Java or Node versions, deliberate but a cost on every pull request) and
*repeated for no benefit* (orange). The `×2.0` label is how many times each test ran on average.

Repeats count as *no benefit* when the same test ran:

- inside a security scan, such as a CodeQL job that runs the build's full test suite;
- twice because one workflow runs on both `push` and `pull_request` for the same commit;
- in two different workflows;
- in two jobs of the same workflow that are not matrix legs;
- twice inside one job.

**What should change?** The findings, ranked, with the repository and the waste in test runs or minutes. Open a
repository's detail to see which jobs overlap and which tests repeat.

Repositories whose logs don't name their tests are listed under the chart; their jobs are counted but cannot be
matched.

## 6. AI impact

How much AI writes, reviews and runs code. AI is only counted where it leaves a trace, so real use is higher. People
are never named.

**Headline numbers.** Pull requests that name an AI tool or model, default-branch commits that name AI, pull requests
reviewed by an AI reviewer, and the share of CI time that involves AI (AI review workflows, AI agents' own runs, and
runs on AI-written changes).

**Which AI tools write code?** Pull requests per tool: Claude Code, GitHub Copilot, the Copilot coding agent, Cursor,
Codex, Gemini, Devin, Aider and others. *Tool not named* means a trace such as `Assisted-by:` named a model but no
tool.

**Which AI models are named?** Pull requests that name each model in a commit trailer or description, such as
`Claude Opus 5.5` or `GPT-5.6`.

**Do AI-written changes cause more problems?** For each tool with at least five pull requests, and for pull requests
with no AI named: the share whose first CI run failed, that got "changes requested", or that were reverted or fixed
soon after. The dashed line is the no-AI baseline. A difference is only called better or worse when the 95% intervals
don't overlap; otherwise the page says the difference could be chance. Small groups are listed but not judged.

**Who starts CI runs?** CI time split between people, bots (Dependabot, Renovate and similar), AI agents and
schedules.

**How ready is each repository for AI?** One row per repository: pull requests and commits that name AI, the
instruction files AI agents can read (`AGENTS.md`, `CLAUDE.md`, Copilot instructions, Cursor rules and others), and
Copilot review runs. A repository where AI writes code but there is no instruction file is a candidate for the Solution box's
*Tell AI agents how to build and test*.

## 7. Solution box

Every problem the report found becomes a card: the problem in your numbers, the expected gain, and *How to fix it*,
often with a YAML snippet to paste. Cards are ordered in four phases:

1. **Remove waste:** stop work that adds nothing (duplicate test runs, tests inside scans, double triggers).
2. **Fix what breaks:** make red mean something again (broken deploys, a red main branch, write-all tokens, unpinned
   or outdated actions, flaky downloads).
3. **Speed up:** caching, and slow pipelines under ten minutes.
4. **Prevent:** test visibility, time limits, helpful process checks, release automation, instructions for AI agents,
   and marking AI-assisted commits.

Each card shows a severity (Critical, High, Medium, Low), an area and an effort (*quick fix* or *some work*). The
repository chips on a card focus the page on that repository. Filter by area with the buttons, or use **Copy as
Markdown** to paste the whole list into an issue or a planning document.

Within a phase, cards are ranked by severity against effort, so a critical quick fix comes first; suggestions that
rest on a pattern rather than a measurement rank a little lower. The Test audit runs before any test-related suggestion is made, so a deliberate version matrix
is reported as a cost to weigh, never as waste.

## How it's measured

The last section of the page lists where the data came from (counts and dates), how the numbers were made, and the
limits, in a few lines each. It ends with the command that rebuilds the report for your own repositories. The full
method is in [How it works](how-it-works.md).
