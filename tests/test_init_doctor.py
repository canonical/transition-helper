import yaml

from fakes import FakeHTTP, FakeLP
from transition_helper.config import UserConfig
from transition_helper.doctor import format_checks, run_checks
from transition_helper.init_wizard import html_to_text, run_wizard
from transition_helper.progress import Progress
from transition_helper.workspace import find_workspace


def test_wizard(tmp_path, monkeypatch):
    lp = FakeLP().add("python3-defaults", "3.15.0-1", "proposed")
    lp.private_ppa = True
    (tmp_path / "notes.md").write_text("# porting\nasyncore is gone\n")
    root = tmp_path / "py315"
    root.mkdir()
    answers = iter([
        "",                     # name -> dir name
        "bogus", "",            # series: invalid, then default (current series)
        "nope", "python3-defaults",
        "3.14", "3.15",
        "ppa", "not-a-ppa", "~team/ubuntu/py315",
        "python3-all-dev, dh-python",
        "riscv64",
        "",                     # tags -> [name]
        "https://example.com/porting.html",
        str(tmp_path / "notes.md"),
        "",
    ])
    out = []
    http = FakeHTTP({"https://example.com/porting.html":
                     "<html><head><style>x</style></head><body><p>Removed: asyncore</p></body></html>"})
    # no LP changelog and no source download available: the wizard warns and goes on
    ws = run_wizard(root, UserConfig(), lp, http, Progress(quiet=True),
                    ask=lambda _: next(answers), out=out.append)
    t = ws.transition
    assert (t.name, t.series, t.toolchain_source, t.build_location, t.ppa) == (
        "py315", "tenacious", "python3-defaults", "ppa", "~team/ubuntu/py315")
    assert t.relevant_binaries == ["python3-all-dev", "dh-python"] and t.ignored_arches == ["riscv64"]
    assert t.lp_bug_tags == ["py315"]
    assert any("WARNING" in o and "private" in o for o in out)
    assert any("could not fetch the toolchain changelog" in o for o in out)
    assert t.docs == ["porting.html.txt", "notes.md"]
    assert "Removed: asyncore" in (root / "docs" / "porting.html.txt").read_text()
    assert find_workspace(root / "docs").transition == t
    saved = yaml.safe_load((root / "transition.yaml").read_text())
    assert saved["docs"] == ["porting.html.txt", "notes.md"]
    for sub in ("docs", "kb", "reports", ".cache"):
        assert (root / sub).is_dir()


def test_html_to_text():
    assert html_to_text("<script>x()</script><p>a</p><p>b</p>").strip() == "a\nb"


def test_doctor(workspace, monkeypatch):
    monkeypatch.chdir(workspace.root)
    monkeypatch.setenv("DEBFULLNAME", "A")
    monkeypatch.setenv("DEBEMAIL", "a@b")
    checks = run_checks(UserConfig(), None, ping_llm=False, lp_factory=lambda: FakeLP())
    by = {c.name: c for c in checks}
    assert by["workspace"].ok and by["Launchpad"].ok and by["maintainer"].ok
    assert by["LLM"].ok is None  # no model configured
    assert by["knowledge base"].ok
    text = format_checks(checks)
    assert "[ok  ] tool git" in text
    cfg = UserConfig()
    cfg.llm.model, cfg.llm.api_key_env = "x/y", "TH_TEST_MISSING_KEY"
    monkeypatch.delenv("TH_TEST_MISSING_KEY", raising=False)
    by = {c.name: c for c in run_checks(cfg, None, lp_factory=lambda: FakeLP())}
    assert by["LLM"].ok is False and "TH_TEST_MISSING_KEY" in by["LLM"].detail
