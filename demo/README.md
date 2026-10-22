# Demo data

The data behind the [live demo](https://kirbac1.github.io/ci-observatory/demo/): 19 repositories of the `aiven` and
`Aiven-Open` GitHub organisations, 3 September to 18 October 2026, collected from public GitHub data with:

```sh
ci-observatory --org aiven --org Aiven-Open --visibility public --top 16 --days 45
```

| File | Contents |
| --- | --- |
| `about.json` | the window, the organisations and when it was collected |
| `runs.json` | 5,620 workflow runs |
| `repos.json` | per-repository summary and workflow hygiene |
| `logs.json` | 116 decoded job logs (cleaned; no raw logs) |
| `tests.json` | the test audit of one pull request commit per repository |
| `signals.json` | AI and DORA signals: pull requests, commits, reviews, releases |
| `fixes.json` | fixes tried and measured on `Aiven-Open/pghoard` in a fork, shown in the Solution box |

The Pages workflow rebuilds the demo from these files with the current page on every push to `main`, so changes to the
page show up in the demo without calling GitHub. To build it locally:

```sh
sh scripts/build-site.sh && python3 -m http.server -d _site
```

The data is a snapshot and is not refreshed. CI Observatory is not affiliated with or endorsed by Aiven.
