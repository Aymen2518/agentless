from pathlib import Path

import pytest

from agentless.config.sources import build_sources
from agentless.config.variables import Resolver, VariableError


def resolve(
    raw: dict, *, stage: str = "dev", options: dict | None = None, params: dict | None = None, root: Path = Path(".")
) -> dict:
    sources = build_sources(root=root, stage=stage, options=options or {}, params=params or {})
    return Resolver(raw, sources).resolve_all()


def test_self_reference_and_shorthand():
    out = resolve({"a": {"b": "x"}, "c": "${self:a.b}-${a.b}", "d": "${stage}"}, stage="uat")
    assert out["c"] == "x-x"
    assert out["d"] == "uat"


def test_lone_expression_keeps_native_type():
    out = resolve({"n": 3, "m": "${self:n}", "obj": {"k": 1}, "copy": "${self:obj}"})
    assert out["m"] == 3
    assert out["copy"] == {"k": 1}


def test_embedding_a_mapping_in_a_string_fails():
    with pytest.raises(VariableError, match="cannot embed a dict"):
        resolve({"obj": {"k": 1}, "s": "x-${self:obj}"})


def test_fallback_literals_are_typed():
    out = resolve(
        {
            "a": "${opt:missing, 'dev'}",
            "b": "${opt:missing, 3}",
            "c": "${opt:missing, true}",
            "d": "${opt:missing, null}",
        }
    )
    assert out == {"a": "dev", "b": 3, "c": True, "d": None}


def test_fallback_is_lazy_and_can_be_nested():
    out = resolve(
        {"a": "${opt:stage, ${env:DOES_NOT_EXIST_123}}", "b": "${opt:nope, ${opt:other, 'deep'}}"},
        options={"stage": "prod"},
    )
    assert out == {"a": "prod", "b": "deep"}


def test_nested_key():
    out = resolve({"stages": {"dev": {"params": {"p": "proj"}}}, "x": "${self:stages.${stage}.params.p}"})
    assert out["x"] == "proj"


def test_param_precedence():
    raw = {
        "stages": {"default": {"params": {"a": "d", "b": "d"}}, "dev": {"params": {"a": "dev"}}},
        "a": "${param:a}",
        "b": "${param:b}",
        "c": "${param:c}",
    }
    out = resolve(raw, params={"c": "cli"})
    assert (out["a"], out["b"], out["c"]) == ("dev", "d", "cli")


def test_missing_reports_path():
    with pytest.raises(VariableError) as exc:
        resolve({"agent": {"env": {"X": "${env:DOES_NOT_EXIST_123}"}}})
    assert exc.value.path == "agent.env.X"


def test_cycle_detected():
    with pytest.raises(VariableError, match="circular reference"):
        resolve({"a": "${self:b}", "b": "${self:a}"})


def test_unknown_source():
    with pytest.raises(VariableError, match="unknown variable source 'ssm'"):
        resolve({"a": "${ssm:/x}"})


def test_env_and_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTLESS_TEST", "hello")
    (tmp_path / "shared.yml").write_text("net:\n  attachment: att-1\n")
    out = resolve({"a": "${env:AGENTLESS_TEST}", "b": "${file(./shared.yml):net.attachment}"}, root=tmp_path)
    assert out == {"a": "hello", "b": "att-1"}


def test_file_with_comma_in_fallback(tmp_path):
    out = resolve({"a": "${file(./absent.yml):x, 'a,b'}"}, root=tmp_path)
    assert out["a"] == "a,b"


def test_skipped_subtrees_stay_raw_unless_referenced():
    raw = {"stages": {"dev": {"p": "a"}, "uat": {"p": "${env:DOES_NOT_EXIST_123}"}}, "x": "${self:stages.dev.p}"}
    sources = build_sources(root=Path("."), stage="dev", options={}, params={})
    out = Resolver(raw, sources, skip=frozenset({"stages.uat"})).resolve_all()
    assert out["stages"]["uat"]["p"] == "${env:DOES_NOT_EXIST_123}"
    assert out["x"] == "a"


def test_quoted_fallback_stays_a_string():
    out = resolve({"a": "${env:DOES_NOT_EXIST_123, 'true'}", "b": "${env:DOES_NOT_EXIST_123, '3'}"})
    assert out == {"a": "true", "b": "3"}


def test_self_reference_through_a_variable_parent(tmp_path):
    (tmp_path / "net.yml").write_text("attachment: att-1\n")
    out = resolve({"net": "${file(./net.yml):}", "x": "${self:net.attachment, 'fb'}"}, root=tmp_path)
    assert out["x"] == "att-1"
