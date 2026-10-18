"""Bundle the observatory page and the collected data into one self-contained HTML file.

The page needs no server and no build step: styles, scripts and data are inlined, and charts are plain SVG and canvas.
Only the two web fonts load from Google Fonts, with system fonts as the fallback when offline.

    python -m ci_observatory.report --data-dir ci-observatory/data --out ci-observatory/index.html
"""

import argparse
import html
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from ci_observatory import __version__

JS_FILES = ("obs-core.js", "obs-fleet.js", "obs-deep.js", "obs-ai.js", "obs-main.js")
SKELETON = (
    '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
    "<style>:root{color-scheme:dark}body{margin:0}[hidden]{display:none!important}</style></head><body>\n"
    "{content}\n</body></html>\n"
)
# File names inside a data directory, as written by `ci-observatory`.
DATA_FILES = {
    "runs": "runs.json",
    "repos": "repos.json",
    "about": "about.json",
    "logs": "logs.json",
    "tests": "tests.json",
    "signals": "signals.json",
}


def web(name: str) -> str:
    return resources.files("ci_observatory").joinpath("web", name).read_text(encoding="utf-8")


def load(path: Path | None) -> Any:
    if path is None or not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def bundle(data: dict[str, Any], *, title: str, heading: str, subtitle: str, description: str, document: bool) -> str:
    """The page with styles, scripts and data inlined. `document=False` leaves out the html/head/body skeleton, for
    hosts that add their own (such as a claude.ai artifact)."""
    page = web("page.html")
    for key, value in (
        ("__TITLE__", title),
        ("__HEADING__", heading),
        ("__SUBTITLE__", subtitle),
        ("__DESCRIPTION__", description),
    ):
        page = page.replace(key, html.escape(value))
    page = page.replace("/*__CSS__*/", web("observatory.css"))
    page = page.replace("/*__JS__*/", "\n".join(web(name) for name in JS_FILES))
    # Every "<" in the data becomes <, so no string in it can close the script element.
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("<", "\\u003c")
    page = page.replace("__DATA__", payload)
    return SKELETON.replace("{content}", page) if document else page


def build(
    paths: dict[str, Path | None],
    out: Path,
    *,
    heading: str,
    orgs: str = "",
    command: str = "",
    document: bool = True,
    note: str = "",
) -> Path:
    about_rows = load(paths.get("about")) or [{}]
    about = about_rows[0] if isinstance(about_rows, list) and about_rows else (about_rows or {})
    runs = load(paths.get("runs")) or []
    repos = load(paths.get("repos")) or []
    meta = {
        "heading": heading,
        "orgs": orgs or about.get("organisations") or "",
        "since": about.get("since"),
        "until": about.get("until"),
        "generated_at": about.get("generated_at") or datetime.now(UTC).isoformat(timespec="seconds"),
        "version": __version__,
        "command": command,
        "max_runs": about.get("max_runs"),
        "note": note,
    }
    data = {
        "meta": meta,
        "runs": runs,
        "repos": repos,
        "logs": load(paths.get("logs")),
        "tests": load(paths.get("tests")),
        "signals": load(paths.get("signals")),
    }
    window = f"{meta['since']} to {meta['until']}" if meta["since"] else "recent runs"
    count = f"{len(repos)} repositories" if len(repos) != 1 else "1 repository"
    subtitle = f"GitHub Actions across {count}{' · ' + meta['orgs'] if meta['orgs'] else ''} · {window}"
    description = "CI/CD speed and failures, job logs, repeated tests, DORA metrics and AI use across GitHub repositories."
    text = bundle(data, title=heading, heading=heading, subtitle=subtitle, description=description, document=document)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, help="directory with runs.json, repos.json, logs.json, tests.json, ...")
    for key in DATA_FILES:
        parser.add_argument(f"--{key}", type=Path, help=f"override the {key} file")
    parser.add_argument("--out", type=Path, default=Path("ci-observatory/index.html"))
    parser.add_argument("--heading", default="CI Observatory")
    parser.add_argument("--orgs", default="")
    parser.add_argument("--command", default="", help="the command shown in the report's 'run it yourself' box")
    parser.add_argument("--note", default="", help="a line for the report's footer, such as where the data came from")
    parser.add_argument("--content-only", action="store_true", help="leave out the html/head/body skeleton")
    args = parser.parse_args(argv)
    paths: dict[str, Path | None] = {}
    for key, name in DATA_FILES.items():
        override = getattr(args, key)
        paths[key] = override or (args.data_dir / name if args.data_dir else None)
    if not paths["runs"] or not paths["runs"].exists():
        print("error: no runs.json; pass --data-dir or --runs", file=sys.stderr)
        return 2
    out = build(
        paths,
        args.out,
        heading=args.heading,
        orgs=args.orgs,
        command=args.command,
        document=not args.content_only,
        note=args.note,
    )
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
