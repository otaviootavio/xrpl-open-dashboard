"""Stage report/graph.html and its JSON views for publishing as one multi-file claude.ai artifact.

    uv run ops/stage_graph_publish.py [--ego-months 1]

Writes report/graph_publish/: the page without its own <html>/<head>/<body> wrapper (the artifact host adds
one) and with the vendored libraries loaded from their pinned jsdelivr URLs (the artifact CSP allows only
CDN scripts), plus the views. Ego views are kept for the newest --ego-months months only: a year of them is
about 1 GB, over the artifact limits (64 MB per publish, 256 MB per version). index.json lists only what is
staged. Prints the publish batches (<= 250 files, <= 60 MB each) as JSON for the Artifact tool's `files`.
"""

import argparse
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC, OUT = ROOT / "report", ROOT / "report" / "graph_publish"
CDN = {"vendor/graphology.umd.min.js": "https://cdn.jsdelivr.net/npm/graphology@0.26.0/dist/graphology.umd.min.js",
       "vendor/sigma.min.js": "https://cdn.jsdelivr.net/npm/sigma@3.0.2/dist/sigma.min.js"}
MAX_FILES, MAX_BYTES = 250, 60 * 1024 * 1024


def page() -> str:
    html = (SRC / "graph.html").read_text()
    for local, url in CDN.items():
        html = html.replace(f'src="{local}"', f'src="{url}"')
    html = re.sub(r"<!doctype html>\s*|</?html[^>]*>\s*|</?head>\s*|</?body>\s*|<meta charset[^>]*>\s*|<meta name=\"viewport\"[^>]*>\s*",
                  "", html, flags=re.I)
    return html


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ego-months", type=int, default=1)
    args = ap.parse_args()
    index = json.loads((SRC / "graph" / "index.json").read_text())
    months = sorted({v["month"] for v in index["views"]})
    ego_months = set(months[-args.ego_months:]) if args.ego_months else set()
    views = [v for v in index["views"] if v["kind"] != "ego" or v["month"] in ego_months]
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "graph").mkdir(parents=True)
    (OUT / "graph.html").write_text(page())
    files = ["graph/index.json"]
    for v in views:
        shutil.copy2(SRC / "graph" / v["file"], OUT / "graph" / v["file"])
        files.append(f"graph/{v['file']}")
    if (SRC / "graph" / "loops.json").exists():
        shutil.copy2(SRC / "graph" / "loops.json", OUT / "graph" / "loops.json")
        files.append("graph/loops.json")
    (OUT / "graph" / "index.json").write_text(json.dumps({**index, "views": views}, separators=(",", ":")))
    batches, cur, size = [], [], 0
    for f in files:
        b = (OUT / f).stat().st_size
        if cur and (len(cur) >= MAX_FILES or size + b > MAX_BYTES):
            batches.append(cur)
            cur, size = [], 0
        cur.append(f)
        size += b
    batches.append(cur)
    total = sum((OUT / f).stat().st_size for f in files)
    print(json.dumps({"page": str(OUT / "graph.html"), "root": str(OUT), "files": len(files), "bytes": total,
                      "ego_months": sorted(ego_months), "batches": [{"files": len(b), "first": b[0], "last": b[-1]}
                                                                   for b in batches]}, indent=1))
    (OUT / "batches.json").write_text(json.dumps(batches))


if __name__ == "__main__":
    main()
