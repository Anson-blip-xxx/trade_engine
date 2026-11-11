import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import v2_trend_package_probe as probe


def put(root, name, content):
    path = root.joinpath(*name.split(".")).with_suffix(".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_closure_finds_deferred_and_transitive_missing_modules(tmp_path):
    candidate, reference = tmp_path / "candidate", tmp_path / "reference"
    put(
        candidate, "services.entry", "def run():\n from v2_core.deferred import value\n"
    )
    put(reference, "v2_core.deferred", "from v2_core.second import value\n")
    put(reference, "v2_core.second", "value = 1\n")
    result = probe.inspect_closure(candidate, reference, ["services.entry"])
    assert result["missing_local_modules"] == ["v2_core.deferred", "v2_core.second"]
    assert result["passed"] is False


def test_candidate_imports_take_precedence_over_reference(tmp_path):
    candidate, reference = tmp_path / "candidate", tmp_path / "reference"
    put(candidate, "services.entry", "value = 1\n")
    put(reference, "services.entry", "import v2_core.extra\n")
    put(reference, "v2_core.extra", "value = 2\n")
    result = probe.inspect_closure(candidate, reference, ["services.entry"])
    assert result["passed"] is True
    assert result["modules_inspected"] == 1


def test_relative_import_is_followed_but_external_module_is_not(tmp_path):
    candidate, reference = tmp_path / "candidate", tmp_path / "reference"
    reference.mkdir()
    put(candidate, "services.entry", "from .helper import value\nimport json\n")
    put(candidate, "services.helper", "value = 1\n")
    result = probe.inspect_closure(candidate, reference, ["services.entry"])
    assert result["passed"] is True
    assert result["modules_inspected"] == 2


def test_unknown_entry_and_syntax_failure_are_blocked(tmp_path):
    candidate, reference = tmp_path / "candidate", tmp_path / "reference"
    reference.mkdir()
    put(candidate, "services.entry", "invalid syntax !\n")
    result = probe.inspect_closure(candidate, reference, ["services.entry", "absent"])
    assert result["passed"] is False
    assert result["missing_local_modules"] == ["absent"]
    assert result["invalid_local_modules"] == ["services.entry"]


def test_actual_controller_schedulers_construct_without_io():
    root = Path(__file__).resolve().parents[2]
    result = probe.controller_smoke(root)
    assert result["passed"] is True
    assert result["scheduler_constructions"] == 4
    assert result["database_calls"] == result["exchange_calls"] == 0


@pytest.mark.parametrize(
    "failure", ["private-secret", '{"passed":false,"error_type":"ValueError"}']
)
def test_child_output_is_sanitized(monkeypatch, tmp_path, failure):
    monkeypatch.setattr(
        probe.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout=failure, stderr="private-secret"
        ),
    )
    result = probe.controller_smoke(tmp_path)
    assert result["passed"] is False
    assert "private-secret" not in str(result)


def test_timeout_does_not_leak_command_or_environment(monkeypatch, tmp_path):
    def fail(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("private-command", 30)

    monkeypatch.setattr(probe.subprocess, "run", fail)
    assert probe.controller_smoke(tmp_path) == {
        "passed": False,
        "error_type": "PROBE_FAILED",
    }
