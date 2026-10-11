"""Stage the published copy of the Graph tab and plan its upload within claude.ai artifact limits.

    uv run ops/stage_graph_site.py --out /path/to/stage            # ego views for the latest full month
    uv run ops/stage_graph_site.py --out DIR --ego-month 2026-09

Copies report/graph.html (as the artifact page), the vendored scripts, loops.json, every backbone, community
map and community bundle, and the ego views of one month; writes a filtered index.json; checks the limits
(511 files and 256 MB per version, 16 MB per file) and writes DIR/publish_plan.json: batches of at most
255 files and 60 MB to send, in order, to the same artifact link.
"""

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REPORT = REPO / "report"
MAX_FILES, MAX_BYTES, MAX_FILE = 511, 256 * 2**20, 16 * 2**20
BATCH_FILES, BATCH_BYTES = 255, 60 * 2**20


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--ego-month", help="YYYY-MM whose ego views are published (default: latest full month)")
    args = ap.parse_args()

    src = REPORT / "graph"
    index = json.loads((src / "index.json").read_text())
    months = sorted({v["month"] for v in index["views"]})
    this_month = datetime.now(timezone.utc).strftime("%Y-%m")
    ego_month = args.ego_month or max(m for m in months if m < this_month)

    out = args.out
    if out.exists():
        shutil.rmtree(out)
    (out / "graph").mkdir(parents=True)
    (out / "vendor").mkdir()

    keep = [v for v in index["views"] if v["label"] != "all" and (v["kind"] != "ego" or v["month"] == ego_month)]   # the page has no all-assets view
    for v in keep:
        shutil.copy2(src / v["file"], out / "graph" / v["file"])
    shutil.copy2(src / "loops.json", out / "graph" / "loops.json")
    (out / "graph" / "index.json").write_text(json.dumps({**index, "views": keep}, separators=(",", ":")))
    for f in ("graphology.umd.min.js", "sigma.min.js"):
        shutil.copy2(REPORT / "vendor" / f, out / "vendor" / f)

    page = (REPORT / "graph.html").read_text()
    note = f"This published copy has ego views for {ego_month} only."
    for old, new in [
        ('No precomputed ego view for this entity. Build one with<br><code>uv run graph/run.py ego ${centre} '
         '--month ${state.month} --asset ${state.label.split("-")[0]}</code>', f"No ego view for this entity here. {note}"),
        ('No precomputed ego view for <code>${q}</code> in ${state.month}. Accounts inside a grouped entity use the '
         'entity id. Build one with<br><code>uv run graph/run.py ego ${q} --month ${state.month}</code>',
         f"No ego view for <code>${{q}}</code> in ${{state.month}}. {note} Accounts inside a grouped entity use the entity id."),
    ]:
        if old not in page:
            sys.exit(f"graph.html changed: hint text not found ({old[:40]}...)")
        page = page.replace(old, new)
    (out / "graph.html").write_text(page)

    files = sorted(p for p in out.rglob("*") if p.is_file())
    total = sum(p.stat().st_size for p in files)
    big = [p for p in files if p.stat().st_size > MAX_FILE]
    problems = []
    if len(files) > MAX_FILES:
        problems.append(f"{len(files)} files > {MAX_FILES}")
    if total > MAX_BYTES:
        problems.append(f"{total / 2**20:.0f} MB > {MAX_BYTES / 2**20:.0f} MB")
    if big:
        problems.append(f"files over 16 MB: {[p.name for p in big]}")

    rel = [str(p.relative_to(out)) for p in files if p.name != "graph.html"]
    batches, cur, cur_bytes = [], [], 0
    for r in rel:
        b = (out / r).stat().st_size
        if cur and (len(cur) >= BATCH_FILES or cur_bytes + b > BATCH_BYTES):
            batches.append(cur)
            cur, cur_bytes = [], 0
        cur.append(r)
        cur_bytes += b
    if cur:
        batches.append(cur)
    (out / "publish_plan.json").write_text(json.dumps({"page": "graph.html", "ego_month": ego_month,
                                                       "batches": batches}, indent=1))
    print(f"staged {len(files)} files, {total / 2**20:.1f} MB, ego views for {ego_month}; "
          f"{len(batches)} publish batches -> {out / 'publish_plan.json'}")
    if problems:
        sys.exit("over artifact limits: " + "; ".join(problems))


if __name__ == "__main__":
    main()
