import pytest
import yaml

from transition_helper.config import ConfigError, parse_config
from transition_helper.workspace import (TRANSITION_FILE, Transition, WorkspaceError,
                                         WorkspaceNotFound, find_workspace, find_workspace_root)


def test_find_workspace_walks_up(workspace):
    deep = workspace.root / "reports" / "foo" / "x"
    deep.mkdir(parents=True)
    assert find_workspace_root(deep) == workspace.root.resolve()
    ws = find_workspace(deep)
    assert ws.transition.name == "python3.15"


def test_find_workspace_stops_at_first(workspace, transition):
    inner = workspace.root / "nested"
    inner.mkdir()
    (inner / TRANSITION_FILE).write_text(yaml.safe_dump(
        Transition(**{**transition.__dict__, "name": "inner"}).to_dict()))
    assert find_workspace(inner / ".").transition.name == "inner"


def test_find_workspace_not_found(tmp_path):
    with pytest.raises(WorkspaceNotFound):
        find_workspace_root(tmp_path)


def test_transition_validation():
    with pytest.raises(WorkspaceError, match="ppa is required"):
        Transition.from_dict({"name": "x", "series": "s", "toolchain_source": "t",
                              "old_version": "1", "new_version": "2", "build_location": "ppa"})
    with pytest.raises(WorkspaceError, match="unknown keys"):
        Transition.from_dict({"name": "x", "bogus": 1})
    t = Transition.from_dict({"name": "x", "series": "s", "toolchain_source": "t",
                              "old_version": 3.14, "new_version": 3.15,
                              "build_location": "ppa", "ppa": "~me/ubuntu/test"})
    assert t.old_version == "3.14" and t.ppa == "~me/ubuntu/test"


def test_report_dirs_unique(workspace):
    from datetime import datetime, timezone

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    a = workspace.new_report_dir("foo", now)
    b = workspace.new_report_dir("foo", now)
    assert a != b and a.name == "20260101T000000Z"


def test_config_parse(monkeypatch):
    cfg = parse_config({"llm": {"model": "m", "api_key_env": "K"}, "budgets": {"max_iterations": 5},
                        "global_kb_path": "~/kb", "allow_private_data": True})
    assert cfg.budgets.max_iterations == 5 and cfg.budgets.max_tokens > 0
    assert cfg.allow_private_data and str(cfg.global_kb_path).endswith("/kb")
    monkeypatch.delenv("DEBFULLNAME", raising=False)
    monkeypatch.delenv("DEBEMAIL", raising=False)
    with pytest.raises(ConfigError):
        cfg.maintainer()
    monkeypatch.setenv("DEBFULLNAME", "A B")
    monkeypatch.setenv("DEBEMAIL", "a@b")
    assert cfg.maintainer() == ("A B", "a@b")
    with pytest.raises(ConfigError):
        parse_config({"budgets": {"max_iterations": "lots"}})
