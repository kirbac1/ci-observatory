# Run it in GitHub Actions

A scheduled workflow can build the report every week and keep it as a workflow artifact. This works for private
repositories, because the report never leaves GitHub.

- [One repository, with the workflow's own token](#one-repository-with-the-workflows-own-token)
- [A whole organisation, with a GitHub App](#a-whole-organisation-with-a-github-app)
- [With a fine-grained token instead of an app](#with-a-fine-grained-token-instead-of-an-app)
- [Getting the report](#getting-the-report)
- [Tips](#tips)

## One repository, with the workflow's own token

Copy [`examples/ci-observatory.yml`](../examples/ci-observatory.yml) to `.github/workflows/ci-observatory.yml` in the
repository you want to watch. That's all. It runs on Mondays at 06:17 UTC and whenever you start it from the Actions
tab.

```yaml
name: CI Observatory

on:
  schedule:
    - cron: "17 6 * * 1" # Mondays 06:17 UTC
  workflow_dispatch:

permissions:
  actions: read # workflow runs, jobs and job logs
  contents: read # workflow files, commits, releases
  pull-requests: read # pull requests, their commits and reviews

concurrency:
  group: ci-observatory
  cancel-in-progress: true

jobs:
  report:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      # gh, Python and pipx come with GitHub-hosted runners, so nothing needs installing.
      - name: Build the report
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          pipx run --spec "git+https://github.com/kirbac1/ci-observatory@v0.1.0" \
            ci-observatory --repo "${{ github.repository }}" --days 30

      - name: Upload the report
        uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2
        with:
          name: ci-observatory
          path: ci-observatory/
          retention-days: 14
```

Why it is built this way:

- **`permissions`** gives the job's token read access to exactly the three things the tool reads, and nothing else.
  Without this block, the token gets your repository's default permissions, which may include write access.
- **`GH_TOKEN: ${{ github.token }}`** hands that token to `gh`. The tool itself never sees it.
- **`pipx run`** needs no install step: `gh`, Python and pipx are already on GitHub-hosted runners. On a self-hosted
  runner, install them first.
- **`@v0.1.0`** pins a release, so the report doesn't change under you. Use a commit SHA for the strictest pinning.
- **`timeout-minutes`** stops a stuck run long before GitHub's 6-hour default.

The built-in token can only read the repository the workflow runs in. Asking it for `--org` sees just that one
repository.

## A whole organisation, with a GitHub App

To cover many repositories, private ones included, from one workflow, give the workflow a short-lived token from a
GitHub App that you install on those repositories. App tokens expire after an hour and are not tied to a person.

1. **Create the app.** In the organisation, open **Settings → Developer settings → GitHub Apps → New GitHub App**.
   - Name: for example *CI Observatory (read-only)*.
   - Homepage URL: your organisation's URL, or this repository's.
   - Webhook: untick **Active**; the app needs no events.
   - **Repository permissions:** Actions, Contents and Pull requests set to **Read-only**. Metadata becomes read-only
     automatically. No organisation permissions are needed: the app's token lists the repositories it is installed on.
   - **Where can this app be installed:** *Only on this account*.
2. **Create a private key.** On the app's page, under *Private keys*, click **Generate a private key**. A `.pem` file
   downloads.
3. **Install the app.** On the app's page, click **Install App**, choose the organisation, then *All repositories* or
   the ones you want in the report.
4. **Store the credentials** in the repository that will run the workflow (or as organisation-level values):
   - a variable `OBSERVATORY_APP_ID` with the app's *App ID* (Settings → Secrets and variables → Actions → Variables);
   - a secret `OBSERVATORY_APP_KEY` with the whole contents of the `.pem` file.
5. **Add the workflow:**

   ```yaml
   name: CI Observatory (organisation)

   on:
     schedule:
       - cron: "17 6 * * 1"
     workflow_dispatch:

   permissions:
     contents: read

   jobs:
     org-report:
       runs-on: ubuntu-latest
       timeout-minutes: 60
       steps:
         - id: app
           uses: actions/create-github-app-token@64ab729a6294000f5aa4a4bc0b73d1f32bc45510 # v3.3.0
           with:
             app-id: ${{ vars.OBSERVATORY_APP_ID }}
             private-key: ${{ secrets.OBSERVATORY_APP_KEY }}
             owner: ${{ github.repository_owner }}

         - name: Build the report
           env:
             GH_TOKEN: ${{ steps.app.outputs.token }}
           run: |
             pipx run --spec "git+https://github.com/kirbac1/ci-observatory@v0.1.0" \
               ci-observatory --org "${{ github.repository_owner }}" --top 20 --days 30

         - uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2
           with:
             name: ci-observatory
             path: ci-observatory/
             retention-days: 14
   ```

An App token has its own rate limit (at least 5,000 requests an hour, more in large organisations), so a weekly report
of 20 to 30 repositories fits comfortably.

## With a fine-grained token instead of an app

Simpler to set up, but the token belongs to a person and must be rotated by hand. Create one as in
[Private repositories, option B](private-repositories.md#option-b-a-fine-grained-personal-access-token), store it as a
secret `OBSERVATORY_TOKEN`, and use it in place of the app step:

```yaml
      - name: Build the report
        env:
          GH_TOKEN: ${{ secrets.OBSERVATORY_TOKEN }}
        run: |
          pipx run --spec "git+https://github.com/kirbac1/ci-observatory@v0.1.0" \
            ci-observatory --org my-org --top 20
```

## Getting the report

Open the workflow run in the Actions tab, scroll to **Artifacts**, download `ci-observatory`, unzip it and open
`index.html`. Only people with read access to the repository can download it, and it is deleted after the retention
period.

The `data/` folder next to `index.html` holds the JSON the report was built from, if you want to feed it into something
else.

## Tips

- **Don't publish a private report to GitHub Pages.** Pages sites are public on most plans. Keep private reports as
  artifacts, or publish them somewhere with access control.
- **Cache job logs between runs** to make weekly runs faster and lighter on the API. Logs are cached in
  `~/.cache/ci-observatory`:

  ```yaml
      - uses: actions/cache@55cc8345863c7cc4c66a329aec7e433d2d1c52a9 # v6.1.0
        with:
          path: ~/.cache/ci-observatory
          key: ci-observatory-${{ github.run_id }}
          restore-keys: ci-observatory-
  ```

- **Post a summary.** The Solution box has a *Copy as Markdown* button; to automate the same, read
  `ci-observatory/data/*.json` with a short script and write to `$GITHUB_STEP_SUMMARY`.
- **Exit code 1** means the report was built but some data was missing (a log had expired, or a call budget ran out).
  The step still succeeds only on exit code 0; add `|| [ $? -eq 1 ]` after the command if gaps should not fail the job.
