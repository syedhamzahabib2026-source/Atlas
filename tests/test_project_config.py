"""Tier 2/3 — per-project quality gate and GitHub config parsing."""

from __future__ import annotations

from pathlib import Path

from core.config import load_config


def test_project_fields_parsed(tmp_path: Path):
    cfg_file = tmp_path / "atlas.yaml"
    cfg_file.write_text(
        """
projects:
  demo:
    repo_path: "C:/tmp/demo"
    test_command: "npm test"
    build_command: "npm run build"
    check_timeout_sec: 120
    verify_url: "http://localhost:3000"
    github_owner: "acme"
    github_repo: "demo"
""",
        encoding="utf-8",
    )
    cfg = load_config(root_dir=tmp_path, config_path=cfg_file, dotenv_path=tmp_path / ".env")
    proj = cfg.projects["demo"]
    assert proj.test_command == "npm test"
    assert proj.build_command == "npm run build"
    assert proj.check_timeout_sec == 120
    assert proj.verify_url == "http://localhost:3000"
    assert proj.github_owner == "acme"
    assert proj.github_repo == "demo"


def test_project_fields_default_empty(tmp_path: Path):
    cfg_file = tmp_path / "atlas.yaml"
    cfg_file.write_text(
        """
projects:
  bare:
    repo_path: "C:/tmp/bare"
""",
        encoding="utf-8",
    )
    cfg = load_config(root_dir=tmp_path, config_path=cfg_file, dotenv_path=tmp_path / ".env")
    proj = cfg.projects["bare"]
    assert proj.test_command == ""
    assert proj.build_command == ""
    assert proj.verify_url == ""
    assert proj.github_owner == ""


def test_claude_kill_session_on_success_default(tmp_path: Path):
    cfg_file = tmp_path / "atlas.yaml"
    cfg_file.write_text("{}", encoding="utf-8")
    cfg = load_config(root_dir=tmp_path, config_path=cfg_file, dotenv_path=tmp_path / ".env")
    assert cfg.claude_code.kill_session_on_success is True
    assert cfg.claude_code.kill_session_on_finish is False
