"""The snapshot as a phone page and as the owner issue's text. Pure: the same sections in the same order as `nzfleet status`."""

from __future__ import annotations

import html as markup

from . import redact, render

REFRESH_SECONDS = 20
ISSUE_TITLE = "nzfleet status"
ISSUE_EVERY = 900.0
STYLE = """
body{font:16px/1.4 system-ui,sans-serif;margin:0;padding:12px;background:#fafafa;color:#1a1a1a;max-width:46em}
h2{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#666;margin:20px 0 6px}
.head{font-size:17px;font-weight:600}.red{color:#b3261e}.green{color:#1b7f3b}
.row{padding:8px 0;border-top:1px solid #e4e4e4}.row small{display:block;color:#666}
code,button{font:13px ui-monospace,monospace}
button{display:block;width:100%;margin-top:6px;padding:12px;border:1px solid #bbb;border-radius:8px;background:#fff;text-align:left;word-break:break-all}
button.done{border-color:#1b7f3b}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:4px 6px 4px 0;text-align:left;vertical-align:top}
details summary{color:#666;cursor:pointer}
@media(prefers-color-scheme:dark){body{background:#111;color:#eee}button{background:#1c1c1c;color:#eee;border-color:#444}.row{border-color:#2a2a2a}h2,.row small{color:#999}}
"""
COPY = """
document.addEventListener('click',function(e){var b=e.target.closest('button[data-copy]');if(!b)return;
var t=b.getAttribute('data-copy'),ok=false;
if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t);ok=true}
else{var a=document.createElement('textarea');a.value=t;document.body.appendChild(a);a.select();try{ok=document.execCommand('copy')}catch(x){}document.body.removeChild(a)}
if(ok){b.classList.add('done');b.firstChild.nodeValue='copied: '+t}});
"""


def _e(value) -> str:
    return markup.escape(str(value), quote=True)


def _headline(h: dict) -> str:
    tone = "" if h["main_green"] is None else "green" if h["main_green"] else "red"
    return f'<p class="head {tone}">{_e(render.headline(h))}</p>'


def _action(row: dict) -> str:
    if not row["fix"]:
        return ""
    return f'<button type="button" data-copy="{_e(row["fix"])}">{_e(row["fix"])}</button>'


def _lines(title: str, rows: list, empty: str) -> str:
    body = "".join(f'<div class="row">{_e(r["text"])}{_action(r)}</div>' for r in rows) or f'<div class="row">{_e(empty)}</div>'
    return f"<h2>{_e(title)}</h2>{body}"


def _pre(title: str, lines: list) -> str:
    return f"<h2>{_e(title)}</h2><pre style=\"white-space:pre-wrap;font-size:13px;margin:0\">{_e(chr(10).join(lines[1:]))}</pre>"


def _agents(rows: list) -> str:
    cells = "".join(
        f"<tr><td>{_e(a['key'])}<small><br>{_e(a['state'])} {_e(a['model'])} {_e(render._span(a['age']))}</small></td>"
        f"<td>{a['cores']:.1f} cpu</td><td>${a['usd']:.2f}</td><td>{_e((a['phase'] + ': ' + a['note']) if a['phase'] else 'no note yet')}</td></tr>"
        for a in rows)
    return f"<h2>Agents</h2><table>{cells or '<tr><td>none</td></tr>'}</table>"


def _held(h: dict) -> str:
    rows = "".join(f'<div class="row">{_e(x["key"])} {_e(render._span(x["age"]))}: {_e(x["why"])}</div>' for x in h["holds"])
    rows += "".join(f'<div class="row">{_e(x["key"])} not dispatched: {_e(x["reason"])}</div>' for x in h["refused"])
    return f"<details><summary>Held and stale: {len(h['holds'])} held, {len(h['refused'])} refused</summary>{rows}</details>"


def html(shot: dict, literals: tuple = ()) -> str:
    """The whole page. Everything shown is escaped, and scrubbed of credentials before that."""
    shot = redact.tree(shot, literals)
    body = [
        _headline(shot["headline"]),
        _lines("Needs you", shot["needs_you"], "nothing"),
        _lines("Needs attention", shot["attention"], "nothing"),
        _pre("Pipeline", render.pipeline(shot["pipeline"])),
        _agents(shot["agents"]),
        _pre("Machine", render.machine(shot["machine"])),
        _pre("Cost", render.cost(shot["cost"])),
        _held(shot["held"]),
    ]
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>nzfleet</title>'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta http-equiv="refresh" content="{REFRESH_SECONDS}"><style>{STYLE}</style></head><body>'
            f'{"".join(body)}<p><small>snapshot taken <time datetime="{int(shot["at"])}">{int(shot["at"])}</time> (epoch s)</small></p>'
            f'<script>{COPY}</script></body></html>')


def issue_text(shot: dict, url: str, literals: tuple = ()) -> str:
    """The headline and the two sections that need a person, as the markdown body of the owner issue."""
    shot = redact.tree(shot, literals)
    out = [render.headline(shot["headline"]), ""]
    for title, rows in (("Needs you", shot["needs_you"]), ("Needs attention", shot["attention"])):
        out.append(f"**{title}**")
        out += [f"- {r['text']}" + (f"\n  `{r['fix']}`" if r["fix"] else "") for r in rows] or ["- nothing"]
        out.append("")
    out.append(f"Everything else: {url}")
    return "\n".join(out)


def issue_due(body: str, last: dict, now: float, every: float = ISSUE_EVERY) -> bool:
    """Whether the issue is worth an edit: what needs a person changed, or the figures in the headline have aged `every` seconds."""
    core = body.split("\n\n", 1)[1] if "\n\n" in body else body
    return last.get("core") != core or now - last.get("at", 0.0) >= every
