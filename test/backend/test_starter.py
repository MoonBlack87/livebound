from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from backend import cli, starter


@pytest.mark.parametrize(
    ("version", "supported"),
    [((3, 11, 9), False), ((3, 12, 0), True), ((3, 13, 1), True)],
)
def test_the_python_floor_is_one_shared_decision(version, supported):
    assert starter.python_is_supported(version) is supported
    assert starter.python_floor_label() == "3.12"


def _frontend(root: Path, *, node_modules: bool = False) -> tuple[Path, Path, Path]:
    frontend = root / "frontend"
    source = frontend / "src" / "main.ts"
    dist = frontend / "dist" / "index.html"
    source.parent.mkdir(parents=True)
    dist.parent.mkdir(parents=True)
    source.write_text("source")
    dist.write_text("build")
    if node_modules:
        (frontend / "node_modules").mkdir()
    return frontend, source, dist


def test_a_released_clone_uses_its_build_without_installing_node_modules(tmp_path):
    _, source, dist = _frontend(tmp_path)
    os.utime(dist, (1, 1))
    os.utime(source, (2, 2))

    plan = starter.frontend_plan(tmp_path, npm_available=True)

    assert not plan.install_dependencies
    assert not plan.build


def test_a_developer_checkout_rebuilds_when_a_source_is_newer(tmp_path):
    _, source, dist = _frontend(tmp_path, node_modules=True)
    os.utime(dist, (1, 1))
    os.utime(source, (2, 2))

    plan = starter.frontend_plan(tmp_path, npm_available=True)

    assert plan.install_dependencies
    assert plan.build


def test_deleting_a_source_makes_the_frontend_stale(tmp_path):
    _, source, dist = _frontend(tmp_path, node_modules=True)
    os.utime(source, (1, 1))
    os.utime(source.parent, (1, 1))
    os.utime(dist, (2, 2))
    assert starter.frontend_is_stale(tmp_path) is False

    source.unlink()

    assert starter.frontend_is_stale(tmp_path) is True


def test_dev_installs_dependencies_but_does_not_make_a_production_build(tmp_path):
    _frontend(tmp_path)

    plan = starter.frontend_plan(tmp_path, npm_available=True, dev=True)

    assert plan.install_dependencies
    assert not plan.build


def test_no_npm_uses_an_existing_build_and_refuses_a_missing_one(tmp_path):
    _frontend(tmp_path)
    existing = starter.frontend_plan(tmp_path, npm_available=False)
    assert existing.warning == starter.NO_NPM_WARNING
    assert existing.error is None

    (tmp_path / "frontend" / "dist" / "index.html").unlink()
    missing = starter.frontend_plan(tmp_path, npm_available=False)
    assert missing.error == starter.NO_BUILD_ERROR
    assert missing.warning is None


def test_skip_build_leaves_a_missing_frontend_alone(tmp_path):
    assert starter.frontend_plan(
        tmp_path, npm_available=False, skip_build=True
    ) == starter.FrontendPlan()


def test_dependency_warning_only_applies_to_an_existing_developer_install(tmp_path):
    frontend, _, _ = _frontend(tmp_path)
    lock = frontend / "package-lock.json"
    lock.write_text("lock")
    assert starter.dependency_warning(tmp_path, "webui.bat") is None

    stamp = frontend / "node_modules" / ".install-stamp"
    stamp.parent.mkdir()
    stamp.write_text("")
    os.utime(stamp, (1, 1))
    os.utime(lock, (2, 2))
    assert starter.dependency_warning(tmp_path, "webui.bat") == (
        "Frontend dependencies are behind package-lock.json. "
        "Run webui.bat --update or npm --prefix frontend install."
    )


def test_the_cli_exposes_the_frontend_decisions(tmp_path, monkeypatch, capsys):
    _frontend(tmp_path)
    monkeypatch.setattr(cli.config, "project_root", lambda: tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["livebound", "starter", "frontend", "--npm"]
    )
    assert cli.main() == 0
    assert capsys.readouterr().err == ""

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "livebound",
            "starter",
            "frontend",
            "--npm",
            "--decision",
            "dependencies",
        ],
    )
    assert cli.main() == 1


def test_windows_starter_quotes_python_paths_and_separates_launcher_args():
    script = (Path(__file__).resolve().parents[2] / "webui.bat").read_text()

    assert '"%PYTHON%" -m backend.cli starter python' in script
    assert 'set "PYTHON_OVERRIDE_USED=1"' in script
    assert 'set "PYTHON_BIN=py"\n    set "PYTHON_ARGS=-3.13"' in script
    assert 'set "PYTHON_BIN=py"\n    set "PYTHON_ARGS=-3.12"' in script
    assert 'set "PYTHON_BIN=python"' in script
    assert script.count('"%PYTHON_BIN%" %PYTHON_ARGS% -m venv') == 2
    assert script.count('uv venv --python "%PYTHON_BIN%"') == 2

    assert '\n    %PYTHON_BIN% -m venv' not in script
    assert '\n    %PYTHON% -m backend.cli starter python' not in script
