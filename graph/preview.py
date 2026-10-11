"""Render a view JSON to a PNG roughly as report/graph.html draws it at default zoom (development aid).

    uv run graph/preview.py report/graph/backbone_2026-09_XRP.json /tmp/bb.png
    uv run graph/preview.py report/graph/community_detail_2026-09_XRP.json /tmp/c0.png --community C0
    uv run graph/preview.py VIEW.json OUT.png --all-edges     # also draw edges with bb = 0

The reference stage is 1,000 x 700 px with sigma's 30 px padding (layout.PX_PER_UNIT); colours follow the
page's light theme. Labels show for label_rank < 30, as on the page.
"""

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import layout

W, H = 1000, 700
ROLE = {"issuer": (42, 120, 214), "likely_custodial": (235, 104, 52), "amm_pool": (27, 175, 122)}
COMM = [(42, 120, 214), (235, 104, 52), (27, 175, 122)]
OTHER, EDGE, INK = (165, 174, 170), (185, 195, 191), (68, 82, 78)


def render(view: dict, out: Path, all_edges: bool = False) -> dict:
    nodes = view["nodes"]
    kind = view.get("attributes", {}).get("kind")
    pos = layout.normalise(np.array([[n["attributes"]["x"], n["attributes"]["y"]] for n in nodes], float))
    px = {n["key"]: (W / 2 + x * layout.PX_PER_UNIT, H / 2 - y * layout.PX_PER_UNIT) for n, (x, y) in zip(nodes, pos)}
    img = Image.new("RGB", (W, H), (252, 253, 252))
    d = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=11)
    drawn = 0
    for e in view["edges"]:
        a = e["attributes"]
        if not all_edges and a.get("bb", 1) == 0:
            continue
        drawn += 1
        d.line([px[e["source"]], px[e["target"]]], fill=EDGE, width=max(1, round(a.get("size", 1) * 0.8)))
    for n in sorted(nodes, key=lambda n: n["attributes"]["size"]):
        a = n["attributes"]
        x, y = px[n["key"]]
        r = a["size"]
        if kind == "communities":
            c = a.get("community")
            col = COMM[c] if isinstance(c, int) and c < 3 else OTHER
        else:
            col = ROLE.get(a.get("role"), OTHER)
        d.ellipse([x - r, y - r, x + r, y + r], fill=col, outline=(252, 253, 252))
    for n in nodes:
        a = n["attributes"]
        if a.get("label") and a.get("label_rank", 0) < layout.LABELS_DEFAULT:
            x, y = px[n["key"]]
            d.text((x + a["size"] + 3, y - 6), a["label"], fill=INK, font=font)
    d.text((8, H - 18), f"{len(nodes)} nodes · {drawn} edges drawn", fill=INK, font=font)
    img.save(out)
    return {"nodes": len(nodes), "edges_drawn": drawn, **layout.check_view(view)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("view", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--community", help="key inside a community_detail bundle, e.g. C0")
    ap.add_argument("--all-edges", action="store_true")
    args = ap.parse_args()
    v = json.loads(args.view.read_text())
    if args.community:
        v = v["communities"][args.community]
    print(render(v, args.out, args.all_edges))


if __name__ == "__main__":
    main()
