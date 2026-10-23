"""Takes credentials and the paths of credential files out of text and out of whole snapshots, so nothing the page or the issue shows can leak one."""

from __future__ import annotations

import re

MARK = "[redacted]"
PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
    re.compile(r"\b(?:gh[pousr]_|github_pat_|sk-|xox[bap]-)[A-Za-z0-9_-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]*"),
    re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(r"(?i)(\b(?:token|secret|password|passwd|api[_-]?key|authorization)[=:]\s?)[^\s\"',;]+"),
    re.compile(r"(?i)(?<![\w/])(?:~|/)[\w.@/-]*(?:credential|secret|token|passw|id_rsa|id_ed25519|\.pem\b|\.key\b|gcs-[\w-]+\.json|/\.config/nzfleet/env)[\w.@/-]*"),
)


def text(value: str, literals: tuple = ()) -> str:
    """`value` with every credential shape, and every literal in `literals` (a configured key or token file), replaced."""
    for literal in literals:
        if literal:
            value = value.replace(literal, MARK)
    for pattern in PATTERNS:
        value = pattern.sub(lambda m: (m.group(1) if m.re.groups else "") + MARK, value)
    return value


def tree(value, literals: tuple = ()):
    """The same walk over nested dicts and lists; keys and numbers are left as they are."""
    if isinstance(value, str):
        return text(value, literals)
    if isinstance(value, dict):
        return {k: tree(v, literals) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [tree(v, literals) for v in value]
    return value
