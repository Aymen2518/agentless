"""Human-readable plan output."""

from __future__ import annotations

import typer

from agentless.plan.model import Action, ChangeSet

_STYLE = {
    Action.CREATE: ("+", typer.colors.GREEN),
    Action.UPDATE: ("~", typer.colors.YELLOW),
    Action.REPLACE: ("±", typer.colors.MAGENTA),
    Action.DELETE: ("-", typer.colors.RED),
    Action.NOOP: ("=", None),
}


def render(changeset: ChangeSet, title: str) -> str:
    """Format a changeset as a colourised, indented diff."""
    lines = [typer.style(title, bold=True)]
    for change in changeset.changes:
        symbol, colour = _STYLE[change.action]
        head = f"  {symbol} {change.resource:<18} {change.action.value:<8} {change.summary}"
        lines.append(typer.style(head, fg=colour) if colour else typer.style(head, dim=True))
        lines.extend(f"      {d}" for d in change.details)
        if change.blocked:
            lines.append(typer.style(f"      ✋ {change.blocked}", fg=typer.colors.RED))
    pending = changeset.pending
    counts = {a: sum(1 for c in pending if c.action == a) for a in Action if a != Action.NOOP}
    summary = ", ".join(f"{n} to {a.value}" for a, n in counts.items() if n) or "no changes"
    lines.append(typer.style(f"Plan: {summary}.", bold=True))
    return "\n".join(lines)
