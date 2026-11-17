import lzma
from pathlib import Path

import yaml

from fakes import FakeHTTP, FakeLP
from test_pipeline import OK, cfg
from transition_helper.classify import Category
from transition_helper.excuses import EXCUSES_URL, ExcusesIndex
from transition_helper.progress import Progress
from transition_helper.report import load_report
from transition_helper.triage import TriageOptions, make_services, triage
from transition_helper.watch import classify_watched, find_blocks, watched_sources

FIX = Path(__file__).parent / "fixtures"
LOG = ("https://autopkgtest.ubuntu.com/results/autopkgtest-tenacious/tenacious/{arch}/x/xfoo/"
       "20261101_000000_abcde@/log.gz")
RETRY = ("https://autopkgtest.ubuntu.com/request.cgi?release=tenacious&arch={arch}&package=xfoo"
         "&trigger=python3-defaults%2F3.15.0-1")


def py_defaults_entry(**over):
    entry = {
        "source": "python3-defaults", "item-name": "python3-defaults",
        "old-version": "3.14.0-1", "new-version": "3.15.0-1",
        "migration-policy-verdict": "REJECTED_PERMANENTLY", "reason": ["autopkgtest"],
        "excuses": ["Impossible Depends: python3-defaults -> libxfoo1/2.0-1/amd64",
                    "Impossible Depends: python3-defaults -> libother1/9-1/amd64"],
        "policy_info": {"autopkgtest": {
            "verdict": "REJECTED_PERMANENTLY",
            "xfoo/2.0-1": {
                "amd64": ["REGRESSION", LOG.format(arch="amd64"), None, None, RETRY.format(arch="amd64")],
                "arm64": ["RUNNING", None, None, None, None],
                "riscv64": ["REGRESSION", LOG.format(arch="riscv64"), None, None, None],
            },
            "other/1.0": {"amd64": ["REGRESSION", None, None, None, None]},
        }},
        "dependencies": {"migrate-after": ["xfoo", "zlib"]},
    }
    entry.update(over)
    return entry


def ix(*entries) -> ExcusesIndex:
    return ExcusesIndex.from_data({"generated-date": "2026-11-17 00:00:00", "sources": list(entries)})


def test_watched_sources(transition):
    transition.watch_sources = ["python3.15", "python3-defaults", "xfoo"]
    assert watched_sources(transition, "xfoo") == ["python3-defaults", "python3.15"]
    assert watched_sources(transition, "python3-defaults") == ["python3.15", "xfoo"]


def test_find_blocks():
    excuses = ix(py_defaults_entry())
    looked_up = []

    def bin_to_src(b, a):
        looked_up.append(b)
        return {"libxfoo1": "xfoo", "libother1": "other"}[b]

    res = find_blocks(excuses, "xfoo", ["python3-defaults", "python3.15"], bin_to_src,
                      pkg_versions={"proposed": None, "release": "2.0-1"},
                      ignored_arches={"riscv64"})
    assert res.missing == ["python3.15"] and res.blocked == ["python3-defaults"]
    assert {b.kind for b in res.blocks} == {"migrate-after", "autopkgtest-regression",
                                            "autopkgtest-pending", "uninstallable"}
    # riscv64 ignored; the other package's test is not ours
    assert [(t.test_pkg, t.arch, t.via, t.own) for t in res.failing_tests] == [
        ("xfoo", "amd64", "python3-defaults", True)]
    assert res.failing_tests[0].triggers == ["python3-defaults/3.15.0-1"]
    assert res.failing_tests[0].kind == "own test, blocking python3-defaults"
    assert [t.arch for t in res.pending_tests] == ["arm64"]
    assert res.tested_arches == {"amd64", "arm64", "riscv64"}
    assert res.watched_has_unmigrated  # also waits on zlib
    # only binaries with one of xfoo's versions are resolved on Launchpad
    assert looked_up == ["libxfoo1"]
    assert res.uninstallable == ["libxfoo1/amd64 (needed by python3-defaults; impossible depends on 2.0-1)"]
    cls = classify_watched(res)
    assert cls.categories == [Category.AUTOPKGTEST_REGRESSION, Category.UNINSTALLABLE,
                              Category.AUTOPKGTEST_PENDING]
    assert any("blocks python3-defaults 3.15.0-1" in n for n in cls.notes)


def test_find_blocks_nothing():
    res = find_blocks(ix(py_defaults_entry()), "unrelated", ["python3-defaults"])
    assert res.blocks == [] and res.blocked == [] and res.missing == []


def _svc(workspace, lp, entries):
    data = lzma.compress(yaml.safe_dump({"generated-date": "2026-11-17 00:00:00",
                                         "sources": entries}).encode())
    http = FakeHTTP({EXCUSES_URL.format(series="tenacious"): data,
                     LOG.format(arch="amd64"): "test foo ... FAIL\nAssertionError: boom\n"})
    return make_services(workspace, cfg(), Progress(quiet=True), lp=lp, http=http)


def test_triage_unchanged_package_blocking_toolchain(workspace, monkeypatch):
    """xfoo has nothing in -proposed, but its test regresses with the new python3-defaults."""
    lp = FakeLP().add("xfoo", "2.0-1", "release", builds=[("amd64", OK)])
    lp.binaries["libxfoo1"] = "xfoo"
    svc = _svc(workspace, lp, [py_defaults_entry()])
    monkeypatch.setattr(svc.autopkgtest, "history", lambda *a, **k: [])
    report_dir, data = triage(svc, "xfoo", TriageOptions(no_llm=True))
    r = load_report(report_dir)
    s = r["summary"]
    assert s["state"] == "NOT_REBUILT" and s["phase"] == "migration"
    assert s["category"] == "AUTOPKGTEST_REGRESSION"
    assert s["blocks"] == ["python3-defaults"]
    assert "blocks the migration of python3-defaults" in s["root_cause"]
    assert not any("nothing to do" in x or "no rebuild has been uploaded" in x for x in s["next_steps"])
    assert any("blocks python3-defaults" in x for x in s["next_steps"])
    assert r["watch"]["via_watched"] is True
    # the test log was fetched as evidence and the retry is triggered by python3-defaults
    assert [log["log_id"] for log in r["evidence"]["logs"]] == ["test:xfoo:amd64:20261101_000000_abcde@"]
    cmds = (report_dir / "commands.sh").read_text()
    assert "package=xfoo" in cmds and "trigger=python3-defaults%2F3.15.0-1" in cmds
    md = (report_dir / "report.md").read_text()
    assert "## Blocks migration of python3-defaults" in md
    assert "own test, blocking python3-defaults" in md
    # the LLM would run for this
    from transition_helper.triage import should_run_llm
    assert should_run_llm(data, TriageOptions()) == (True, "autopkgtest regression")


def test_triage_own_excuse_first(workspace):
    """xfoo has its own excuse: that drives the classification; the block is extra info."""
    lp = FakeLP().add("xfoo", "2.0-1", "release").add("xfoo", "2.0-2", "proposed",
                                                     builds=[("amd64", OK)])
    own = {"source": "xfoo", "item-name": "xfoo", "old-version": "2.0-1", "new-version": "2.0-2",
           "migration-policy-verdict": "PASS", "is-candidate": True}
    svc = _svc(workspace, lp, [py_defaults_entry(), own])
    report_dir, data = triage(svc, "xfoo", TriageOptions(no_llm=True))
    r = load_report(report_dir)
    s = r["summary"]
    assert s["state"] == "IN_PROPOSED_WITH_EXCUSE" and s["category"] == "NONE"
    assert s["blocks"] == ["python3-defaults"] and r["watch"]["via_watched"] is False
    assert r["evidence"]["logs"] == []  # secondary: not analysed
    assert any(x.startswith("also: xfoo blocks python3-defaults") for x in s["next_steps"])
    cmds = (report_dir / "commands.sh").read_text()
    assert "regresses in the excuse of python3-defaults" in cmds
    assert "Found in the excuse(s) of python3-defaults" in (report_dir / "report.md").read_text()


def test_triage_not_blocking_keeps_old_behaviour(workspace):
    lp = FakeLP().add("bar", "1.0-1", "release")
    svc = _svc(workspace, lp, [py_defaults_entry()])
    report_dir, _ = triage(svc, "bar", TriageOptions(no_llm=True))
    s = load_report(report_dir)["summary"]
    assert s["category"] == "NONE" and s["blocks"] == []
    assert any("no rebuild has been uploaded" in x for x in s["next_steps"])
