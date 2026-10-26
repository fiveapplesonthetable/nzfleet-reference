#!/usr/bin/env python3
"""Mermaid diagrams in docs/book that read as a list, a wall, or a repeat.

    tools/nzfleet/book/book-diagrams.py [BOOK_DIR]

Flags a flowchart of five or more boxes in one unbranched chain (a numbered list
drawn as boxes), a flowchart of more than 25 nodes (too big for one page), two
diagrams whose labels are mostly the same (a repeat), a diagram with no caption,
and a state diagram with no title. An entry in ALLOWED keeps a diagram anyway,
with the reason.
"""
import glob
import os
import re
import sys

MAX_NODES = 25
CHAIN_MIN = 5
REPEAT_SHARE = 0.6
REPEAT_MIN_LABELS = 4

# "<chapter file>:<diagram number>" -> why it stays.
ALLOWED = {
    "01-what-nzela-is.md:2": "the legend: a row of one of each shape is the point",
}

EDGE = re.compile(r"(-->|-\.->|==>|---|-\.-|--[^->\n]*-->|-\.[^->\n]*\.->)")
NODE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*(?:\[\(|\[\[|\(\[|\[/|\[|\(\(|\(|\{)\s*\"?([^\]\)\}\"]+)")


def blocks(text):
    return [m.group(1) for m in re.finditer(r"```mermaid\n(.*?)```", text, re.S)]


def without_front_matter(body):
    return re.sub(r"\A\s*---\n.*?\n---\n", "", body, flags=re.S)


def parse_flowchart(body):
    labels = {}
    edges = []
    for raw in body.splitlines()[1:]:
        line = re.sub(r"%%.*", "", raw).strip()
        if not line or line.startswith(("classDef", "class ", "style ", "linkStyle", "subgraph", "end", "direction")):
            continue
        for m in NODE.finditer(line):
            labels[m.group(1)] = m.group(2).strip()
        plain = re.sub(r"\[\(.*?\)\]|\[\[.*?\]\]|\(\[.*?\]\)|\[/.*?/\]|\[.*?\]|\(\(.*?\)\)|\(.*?\)|\{.*?\}", "", line)
        plain = re.sub(r"\|[^|]*\|", "", plain)
        plain = re.sub(r":::\w+", "", plain)
        parts = [p.strip() for p in EDGE.split(plain)]
        ids = [re.sub(r"[^A-Za-z0-9_&]", "", p) for p in parts[0::2]]
        for left, right in zip(ids, ids[1:]):
            for a in left.split("&"):
                for b in right.split("&"):
                    if a and b:
                        edges.append((a, b))
                        labels.setdefault(a, a)
                        labels.setdefault(b, b)
    return labels, edges


def is_chain(labels, edges):
    if len(labels) < CHAIN_MIN:
        return False
    out, into = {}, {}
    for a, b in edges:
        out[a] = out.get(a, 0) + 1
        into[b] = into.get(b, 0) + 1
    return all(out.get(n, 0) <= 1 and into.get(n, 0) <= 1 for n in labels)


def words(label):
    return set(re.findall(r"[a-z0-9]+", label.lower()))


def main():
    book = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "docs", "book")
    flagged = []
    seen = []
    for path in sorted(glob.glob(os.path.join(book, "[0-9][0-9]-*.md"))):
        name = os.path.basename(path)
        text = open(path, encoding="utf-8").read()
        for n, body in enumerate(blocks(text), 1):
            key = f"{name}:{n}"
            kind = without_front_matter(body).strip().split()[0]
            problems = []
            after = text.split(body, 1)[1][:400].lstrip("`\n ")
            if not re.match(rf"\*Diagram \d+\.{n}\. ", after):
                problems.append("has no caption in the paragraph after it")
            if kind in ("flowchart", "graph"):
                labels, edges = parse_flowchart(without_front_matter(body))
                if is_chain(labels, edges):
                    problems.append(f"is a chain of {len(labels)} boxes: write it as a numbered list")
                if len(labels) > MAX_NODES:
                    problems.append(f"has {len(labels)} nodes, more than one page holds")
                names = {" ".join(sorted(words(v))) for v in labels.values()}
                if len(labels) >= REPEAT_MIN_LABELS:
                    seen.append((key, names))
            if kind.startswith("stateDiagram") and not re.match(r"\s*---\n(?:.*\n)*?title: ", body):
                problems.append("is a state diagram with no title naming the machine")
            if key in ALLOWED:
                continue
            flagged += [f"{key} {p}" for p in problems]
    for i, (ka, na) in enumerate(seen):
        for kb, nb in seen[i + 1:]:
            if ALLOWED.get(ka) or ALLOWED.get(kb):
                continue
            common = len(na & nb)
            if common / max(1, min(len(na), len(nb))) >= REPEAT_SHARE:
                flagged.append(f"{ka} and {kb} share most of their labels: keep one and link to it")
    for line in flagged:
        print(line)
    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main())
