"""Human-readable plan output."""

from __future__ import annotations

import os

import typer

from agentless.plan.model import Action, ChangeSet

_STYLE = {
    Action.CREATE: ("+", typer.colors.GREEN),
    Action.UPDATE: ("~", typer.colors.YELLOW),
    Action.REPLACE: ("±", typer.colors.RED),  # destroy then recreate, like terraform -/+
    Action.DELETE: ("-", typer.colors.RED),
    Action.NOOP: ("=", None),
}
# Detail lines start with the same markers: + added, ~ changed, - removed, ± replaced, ! warning, = unchanged.
_DETAIL: dict[str, tuple[str | None, bool | None, bool | None]] = {  # marker: (colour, bold, dim)
    "+": (typer.colors.GREEN, None, None),
    "~": (typer.colors.YELLOW, None, None),
    "-": (typer.colors.RED, None, None),
    "±": (typer.colors.RED, None, None),
    "!": (typer.colors.YELLOW, True, None),
    "=": (None, None, True),
}
_CI = ("CI", "GITLAB_CI", "GITHUB_ACTIONS", "BUILDKITE", "TF_BUILD")


def use_color(flag: bool | None = None) -> bool | None:
    """Whether to colour output: --color/--no-color, then NO_COLOR, FORCE_COLOR, CI; None lets click detect a TTY.

    CI logs are not terminals, so click would strip the colours there; GitLab and GitHub render them.
    """
    if flag is not None:
        return flag
    env = os.environ
    if env.get("NO_COLOR"):
        return False
    for name in ("FORCE_COLOR", "CLICOLOR_FORCE"):
        if env.get(name) and env[name] != "0":
            return True
    if any(env.get(name) for name in _CI):
        return True
    return None


def _detail(line: str) -> str:
    if line[:1] not in _DETAIL:
        return line
    fg, bold, dim = _DETAIL[line[:1]]
    return typer.style(line, fg=fg, bold=bold, dim=dim)


def render(changeset: ChangeSet, title: str) -> str:
    """Format a changeset as a colourised, indented diff."""
    lines = [typer.style(title, bold=True)]
    for change in changeset.changes:
        symbol, colour = _STYLE[change.action]
        head = f"  {symbol} {change.resource:<18} {change.action.value:<8} {change.summary}"
        lines.append(typer.style(head, fg=colour, bold=True) if colour else typer.style(head, dim=True))
        lines.extend(f"      {_detail(d)}" for d in change.details)
        if change.blocked:
            lines.append(typer.style(f"      ✋ {change.blocked}", fg=typer.colors.RED, bold=True))
    pending = changeset.pending
    counts = {a: sum(1 for c in pending if c.action == a) for a in Action if a != Action.NOOP}
    parts = [typer.style(f"{n} to {a.value}", fg=_STYLE[a][1], bold=True) for a, n in counts.items() if n]
    summary = ", ".join(parts) or typer.style("no changes", fg=typer.colors.GREEN)
    lines.append(typer.style("Plan: ", bold=True) + summary + typer.style(".", bold=True))
    return "\n".join(lines)
