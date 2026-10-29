"""Plan colours (terraform-like) and when they are turned on."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentless import cli
from agentless.plan.model import Action, Change, ChangeSet
from agentless.plan.render import _CI, render, use_color

GREEN, YELLOW, RED, DIM = "\x1b[32m", "\x1b[33m", "\x1b[31m", "\x1b[2m"


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in ("NO_COLOR", "FORCE_COLOR", "CLICOLOR_FORCE", *_CI):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _line(out: str, text: str) -> str:
    return next(line for line in out.splitlines() if text in line)


def test_actions_and_details_get_terraform_colours() -> None:
    out = render(
        ChangeSet(
            [
                Change("serviceAccount", Action.NOOP, "sa@p.iam.gserviceaccount.com"),
                Change("engine", Action.CREATE, "e", ["+ first deploy"]),
                Change(
                    "service", Action.UPDATE, "s", ["+ env.A = '1'", "~ timeout: '1s' → '2s'", "- env.B", "! careful"]
                ),
                Change("access", Action.DELETE, "1 binding"),
                Change("geminiEnterprise", Action.REPLACE, "ge", ["± registration adk → a2a"]),
                Change("agentRegistry", Action.UPDATE, "card", blocked="public grant"),
            ]
        ),
        "Changes:",
    )
    assert GREEN in _line(out, "engine") and GREEN in _line(out, "+ first deploy")
    assert YELLOW in _line(out, "~ service ") and YELLOW in _line(out, "~ timeout")
    assert GREEN in _line(out, "+ env.A") and RED in _line(out, "- env.B")
    assert RED in _line(out, "access") and RED in _line(out, "geminiEnterprise") and RED in _line(out, "± registration")
    assert YELLOW in _line(out, "! careful") and RED in _line(out, "✋ public grant")
    assert DIM in _line(out, "serviceAccount")
    summary = _line(out, "Plan:")
    assert f"{GREEN}\x1b[1m1 to create" in summary and f"{YELLOW}\x1b[1m2 to update" in summary
    assert f"{RED}\x1b[1m1 to replace" in summary and f"{RED}\x1b[1m1 to delete" in summary


def test_no_changes_summary() -> None:
    out = render(ChangeSet([Change("engine", Action.NOOP, "e")]), "Changes:")
    assert "no changes" in _line(out, "Plan:")


@pytest.mark.parametrize(
    ("env", "flag", "expected"),
    [
        ({}, None, None),  # click decides: colour only on a TTY
        ({"GITLAB_CI": "true"}, None, True),
        ({"GITHUB_ACTIONS": "true"}, None, True),
        ({"FORCE_COLOR": "1"}, None, True),
        ({"FORCE_COLOR": "0"}, None, None),
        ({"CLICOLOR_FORCE": "1"}, None, True),
        ({"NO_COLOR": "1", "GITLAB_CI": "true", "FORCE_COLOR": "1"}, None, False),
        ({"NO_COLOR": "1"}, True, True),  # the flag wins
        ({"GITLAB_CI": "true"}, False, False),
    ],
)
def test_use_color(clean_env: pytest.MonkeyPatch, env: dict, flag: bool | None, expected: bool | None) -> None:
    for name, value in env.items():
        clean_env.setenv(name, value)
    assert use_color(flag) is expected


@pytest.mark.parametrize(("args", "coloured"), [(["--color"], True), (["--no-color"], False), ([], False)])
def test_color_flag_reaches_every_command(clean_env, agent_dir: Path, args: list[str], coloured: bool) -> None:
    result = CliRunner().invoke(cli.app, [*args, "validate", "-c", str(agent_dir / "agent.yaml")])
    assert result.exit_code == 0, result.output
    assert ("\x1b[" in result.output) is coloured


def test_ci_turns_colours_on(clean_env, agent_dir: Path) -> None:
    clean_env.setenv("GITLAB_CI", "true")
    result = CliRunner().invoke(cli.app, ["validate", "-c", str(agent_dir / "agent.yaml")])
    assert "\x1b[32m" in result.output
