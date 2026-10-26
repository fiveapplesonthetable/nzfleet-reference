#!/usr/bin/env python3
"""Render Rust source references in the TLA+ mapping from named declarations."""

from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[3]
MAPPING = ROOT / "formal/tla/MAPPING.md"
TEMPLATE = ROOT / "formal/tla/MAPPING.md.in"
REFERENCES = {
    "status": ("crates/nzela-lifecycle/src/transfer.rs", "pub enum Status"),
    "apply": ("crates/nzela-movement/src/lib.rs", "pub async fn apply("),
    "move_row": ("crates/nzela-movement/src/lib.rs", "async fn move_row("),
    "attempt": ("crates/nzela-movement/src/attempts.rs", "pub async fn begin_attempt("),
    "lock_payout": ("crates/nzela-movement/src/payout_steps.rs", "pub async fn lock_payout("),
    "correction": ("crates/nzela-corrections/src/corrections.rs", "async fn retry_leg("),
    "authorize_send": ("crates/nzela-movement/src/payout_steps.rs", "async fn fence("),
    "finish_send": ("crates/nzela-corrections/src/corrections.rs", "pub async fn finish_payout_send("),
    "sweep_authorize": ("crates/nzela-worker/src/payout/sweep.rs", "async fn authorize("),
    "payout_settle": ("crates/nzela-worker/src/payout.rs", "pub(super) async fn settle_payout("),
    "payout_window": ("crates/nzela-worker/src/payout.rs", "pub(super) async fn settle_payout_at_its_window("),
    "payout_rationed": ("crates/nzela-worker/src/payout.rs", "pub(super) fn rationed("),
    "sweep_lookup": ("crates/nzela-worker/src/payout/sweep.rs", "async fn lookup("),
    "sweep_pay": ("crates/nzela-worker/src/payout/sweep.rs", "async fn pay("),
    "refund_close": ("crates/nzela-worker/src/refund.rs", "pub(super) async fn close_corrections<"),
    "refund_send": ("crates/nzela-worker/src/refund.rs", "pub(super) async fn return_money<"),
    "refund_run": ("crates/nzela-worker/src/refund.rs", "async fn run("),
    "refund_pay": ("crates/nzela-worker/src/refund.rs", "async fn pay("),
    "refund_order": ("crates/nzela-corrections/src/corrections.rs", "pub async fn order_refund<'a>("),
    "notice": ("crates/nzela-api/src/notice.rs", "pub(super) async fn notice<"),
    "webhook_verify": ("crates/nzela-providers/src/bluefin/webhook.rs", "pub(super) fn verify("),
    "expire": ("crates/nzela-worker/src/pass.rs", "async fn expire_quotes("),
    "worker_apply_answer": ("crates/nzela-worker/src/pass.rs", "pub(crate) async fn apply_answer("),
    "apply_heard": ("crates/nzela-movement/src/lib.rs", "pub async fn apply_heard("),
    "moved_on_answer": ("crates/nzela-worker/src/pass.rs", "pub(crate) fn moved_on_answer("),
    "resolve_one": ("crates/nzela-worker/src/lib.rs", "pub async fn resolve_one<"),
    "ask_status": ("crates/nzela-worker/src/ask.rs", "pub(super) async fn ask_status<"),
    "collect_resolve": ("crates/nzela-worker/src/collect.rs", "pub(super) async fn resolve_collections<"),
    "collect_late": ("crates/nzela-worker/src/collect.rs", "pub(super) async fn settle_late_collections<"),
    "lease_claim": ("crates/nzela-pg-transfers/src/lease.rs", "pub async fn claim("),
    "believed": ("crates/nzela-worker/src/ask.rs", "async fn believed<"),
    "posting": ("crates/nzela-movement/src/posting.rs", "pub async fn post("),
    "correct_recipient": ("crates/nzela-corrections/src/corrections.rs", "pub async fn correct_recipient("),
}


def render() -> str:
    text = TEMPLATE.read_text()
    placeholders = re.findall(r"\{\{([^{}]+)\}\}", text)
    if len(placeholders) != len(set(placeholders)) or set(placeholders) != set(REFERENCES):
        raise SystemExit("MAPPING.md.in must use each named source reference exactly once")
    for name, (relative_path, declaration) in REFERENCES.items():
        source = (ROOT / relative_path).read_text().splitlines()
        matches = [index + 1 for index, line in enumerate(source) if declaration in line]
        if len(matches) != 1:
            raise SystemExit(f"{relative_path}: expected one {declaration!r}, found {len(matches)}")
        text = text.replace(f"{{{{{name}}}}}", f"`{relative_path}:{matches[0]}`")
    return text


expected = render()
actual = MAPPING.read_text()
if "--write" in sys.argv:
    MAPPING.write_text(expected)
elif actual != expected:
    print("formal/tla/MAPPING.md has stale generated source line references", file=sys.stderr)
    raise SystemExit(1)
