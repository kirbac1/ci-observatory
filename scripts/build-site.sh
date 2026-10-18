#!/bin/sh
# Build the GitHub Pages site into _site/ (or the folder given as $1):
#   index.html   the landing page, from site/
#   demo/        a live report built from demo/data with the current page template
#   install.sh   the one-line installer
# The Pages workflow runs this; run it locally with: sh scripts/build-site.sh && python3 -m http.server -d _site
set -eu

out="${1:-_site}"
rm -rf "$out"
mkdir -p "$out/demo"
cp -R site/. "$out/"
cp install.sh "$out/install.sh"
uv run python -m ci_observatory.report \
  --data-dir demo/data \
  --out "$out/demo/index.html" \
  --heading "CI Observatory: Aiven's public repositories" \
  --orgs "aiven, Aiven-Open" \
  --command "ci-observatory --org aiven --org Aiven-Open --visibility public --top 16 --days 45" \
  --note "Demo built from public GitHub data of the aiven and Aiven-Open organisations. Not affiliated with Aiven."
touch "$out/.nojekyll"
echo "Built $out: $(find "$out" -type f | wc -l | tr -d ' ') files"
