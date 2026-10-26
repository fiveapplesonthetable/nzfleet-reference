#!/usr/bin/env python3
"""Compare selected capture files and write a reviewable before/after artifact."""

import argparse
import hashlib
import html
import json
from pathlib import Path
import shutil
import struct

AREAS = {
    "console": "frontend/admin/screenshots",
    "android": "frontend/android/docs/screenshots",
}


def details(path):
    if not path.exists():
        return None
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError(f"not a PNG: {path}")
    width, height = struct.unpack(">II", data[16:24])
    return {"sha256": hashlib.sha256(data).hexdigest(), "dimensions": [width, height]}


def image_status(previous, current):
    if previous is None and current is None:
        return "missing"
    if previous == current:
        return "same"
    if previous is None:
        return "new"
    if current is None:
        return "removed"
    return "changed"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    if plan.get("version") != 1:
        parser.error("unsupported plan version")
    if args.out.exists():
        parser.error("artifact directory already exists")
    args.out.mkdir(parents=True)
    rows = []
    for area, directory in AREAS.items():
        selected = set(plan["selected"][area]) | set(plan["deleted"][area])
        if plan["full_fallback"][area]:
            selected |= {p.stem for p in (args.before / directory).glob("*.png")}
            selected |= {p.stem for p in (args.after / directory).glob("*.png")}
        for stem in sorted(selected):
            relative = Path(directory) / f"{stem}.png"
            before = args.before / relative
            after = args.after / relative
            previous, current = details(before), details(after)
            status = image_status(previous, current)
            for label, source in (("before", before), ("after", after)):
                if source.exists():
                    destination = args.out / label / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
            rows.append({"area": area, "stem": stem, "status": status, "before": previous, "after": current})
    (args.out / "result.json").write_text(json.dumps(rows, indent=2) + "\n")
    sections = []
    for row in rows:
        if row["status"] == "same":
            continue
        relative = Path(AREAS[row["area"]]) / f"{row['stem']}.png"
        title = html.escape(f"{row['area']}: {row['stem']} ({row['status']})")
        pictures = "".join(
            f'<figure><figcaption>{label}: {html.escape(str(row[label]))}</figcaption>'
            f'<img src="{html.escape(str(Path(label) / relative))}"></figure>'
            for label in ("before", "after") if row[label] is not None
        )
        if row["before"] is not None and row["after"] is not None:
            old = html.escape(str(Path("before") / relative))
            new = html.escape(str(Path("after") / relative))
            pictures += f'<figure><figcaption>pixel difference overlay</figcaption><div class="overlay"><img src="{old}"><img src="{new}"></div></figure>'
        sections.append(f"<h2>{title}</h2><div class='pair'>{pictures}</div>")
    page = "<html><meta charset='utf-8'><style>body{font:14px system-ui;margin:2rem}.pair{display:flex;gap:1rem}figure{margin:0;max-width:33%}img{max-width:100%;border:1px solid gray}.overlay{position:relative}.overlay img+img{position:absolute;inset:0;mix-blend-mode:difference}</style><h1>Visual diff</h1>" + "".join(sections) + "</html>"
    (args.out / "index.html").write_text(page)
    changed = [row for row in rows if row["status"] != "same"]
    print(json.dumps({"images": len(rows), "unexplained": len(changed), "artifact": str(args.out)}))
    if changed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
