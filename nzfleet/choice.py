"""Which provider and model a queued task asks for, as pure functions of the words the owner typed."""

from __future__ import annotations

CLAUDE_FAMILIES = ("sonnet", "haiku", "opus", "fable")
CODEX_PREFIXES = ("gpt-", "o1", "o3", "o4", "codex")


class Unknown(ValueError):
    """The model name does not say which provider runs it."""


def provider_of(model: str) -> str:
    """The provider a model name implies: Claude family names and `claude-*` are claude, `gpt-*` is codex."""
    if model.startswith("claude-") or model in CLAUDE_FAMILIES:
        return "claude"
    if model.startswith(CODEX_PREFIXES):
        return "codex"
    raise Unknown(f"cannot tell which provider runs '{model}'; add --provider codex or --provider claude")


def resolve(provider: str | None, model: str | None, codex_default: str) -> tuple[str, str] | None:
    """(provider, model) for a task that names either, or None when it names neither and the session decides."""
    if not provider and not model:
        return None
    provider = provider or provider_of(model or "")
    if model is None:
        return provider, codex_default if provider == "codex" else "sonnet"
    if provider_of(model) != provider:
        raise Unknown(f"'{model}' is not a {provider} model")
    return provider, model


def parse_compare(spec: str, codex_default: str) -> list[tuple[str, str]]:
    """`codex:gpt-6-luna,claude:sonnet` as workers; a bare model name infers its provider."""
    workers = []
    for part in filter(None, (p.strip() for p in spec.split(","))):
        provider, _, model = part.partition(":")
        chosen = resolve(provider, model, codex_default) if model else resolve(None, provider, codex_default)
        if chosen and chosen not in workers:
            workers.append(chosen)
    return workers


def worker_suffix(model: str) -> str:
    """The branch and PR suffix that tells compared runs apart."""
    return "".join(c if c.isalnum() else "-" for c in model.lower()).strip("-")


def compare_items(key: str, task: str, area: str, after: list, workers: list[tuple[str, str]]) -> list[dict]:
    """One queue entry per worker, each in its own lane so they run side by side on separate branches."""
    items = []
    for provider, model in workers:
        suffix = worker_suffix(model)
        items.append({"key": f"{key}-{suffix}", "task": compare_task(task, key, suffix, model), "model": model, "provider": provider,
                      "area": f"{area}-{suffix}" if area else "", "after": after,
                      "kind": "task", "pr": None})
    return items


def compare_task(task: str, key: str, suffix: str, model: str) -> str:
    """The brief with the instructions that keep compared runs apart and their PRs drafts."""
    return (f"{task}\n\nThis is one of several runs of the same brief that the owner compares on real work. Use the branch name "
            f"`{key}-{suffix}`, open the PR as a draft and leave it a draft, and end its title with `[{model}]`. Do not look at the "
            f"other runs' branches.")


def label(item: dict) -> str:
    """`codex/gpt-6-luna` for a task that chose its provider, else the model alone."""
    return f"{item['provider']}/{item['model']}" if item.get("provider") else item["model"]
