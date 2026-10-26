import sys
from pathlib import Path

import pytest
import yaml

from conftest import requires_tools
from fakes import FakeHTTP, FakeLP, ScriptedLLM, call, minimal_final
from test_patches import GOOD
from transition_helper.config import Budgets, UserConfig
from transition_helper.excuses import EXCUSES_URL
from transition_helper.filebug import file_bug
from transition_helper.followup import followup
from transition_helper.learn import candidate_patterns, learn
from transition_helper.lp import BugInfo
from transition_helper.progress import Progress
from transition_helper.report import load_report
from transition_helper.triage import TriageOptions, make_services, triage

FIX = Path(__file__).parent / "fixtures"
OK = "Successfully built"
LOG_URL = "https://lp.example/foo/1.0-1build1/amd64/log.txt.gz"


def cfg(**kw):
    c = UserConfig(debfullname="Jane Dev", debemail="jane@example.com",
                   budgets=Budgets(max_iterations=8, max_tokens=100_000, max_tool_result_chars=6000))
    c.llm.model = "fake/model"
    for k, v in kw.items():
        setattr(c, k, v)
    return c


@pytest.fixture
def env(workspace, make_pkg):
    lp = FakeLP().add("foo", "1.0-1", "release").add(
        "foo", "1.0-1build1", "proposed",
        builds=[("amd64", "Failed to build"), ("arm64", "Failed to build"), ("s390x", OK)])
    lp.bugs["foo"] = [BugInfo(1, "foo: FTBFS with python3.15", "New", ["python3.15", "ftbfs"],
                              "https://bugs.launchpad.net/bugs/1")]
    dsc = make_pkg("foo", "1.0-1build1")
    lp.source_files[("foo", "1.0-1build1")] = [f"https://lp.example/src/{p.name}" for p in dsc.parent.glob("foo_*")]
    log = (FIX / "build_foo_amd64.log").read_text()
    routes = {EXCUSES_URL.format(series="tenacious"): (FIX / "update_excuses.yaml.xz").read_bytes(),
              LOG_URL: log, LOG_URL.replace("amd64", "arm64"): log.replace("amd64", "arm64")}
    for p in dsc.parent.glob("foo_*"):
        routes[f"https://lp.example/src/{p.name}"] = p.read_bytes()
    http = FakeHTTP(routes)
    progress = Progress(quiet=True)
    svc = make_services(workspace, cfg(), progress, lp=lp, http=http)
    return svc, lp, http


def ftbfs_script():
    return [
        [call("grep_log", log_id="build:foo:amd64", pattern="No module named")],
        [call("get_source", pkg="foo", reason="find the imp import")],
        [call("propose_patch", target_pkg="foo", unified_diff=GOOD, description="Port away from imp",
              patch_name="port-imp")],
        [call("final_report", **minimal_final(
            fixes=[{"target_pkg": "foo", "target_version": "1.0-1build1",
                    "rationale": "the import is in foo itself", "patch_ref": "port-imp"}],
            kb_matches=["py-removed-imp"],
            bug_draft={"title": "foo: FTBFS with Python 3.15 (imp removed)",
                       "description": "foo imports imp, removed in 3.12+."}))],
    ]


@requires_tools
def test_triage_ftbfs_full(env):
    svc, lp, http = env
    llm = ScriptedLLM(ftbfs_script())
    report_dir, data = triage(svc, "foo", TriageOptions(), llm_client=llm)
    r = load_report(report_dir)
    s = r["summary"]
    assert s["phase"] == "build" and s["category"] == "FTBFS" and s["state"] == "BUILD_FAILED"
    assert s["confidence"] == "medium" and s["is_transition_related"] is True
    assert s["fix_targets"][0]["target_pkg"] == "foo" and s["fix_targets"][0]["patch_file"]
    assert r["kb_matches"] == ["py-removed-imp"]
    # two arches with the same failure -> one representative (amd64)
    assert [g["representative"] for g in r["evidence"]["groups"]] == ["build:foo:amd64"]
    md = (report_dir / "report.md").read_text()
    assert "### Fix target(s)" in md and "**foo**" in md and "py-removed-imp" in md
    assert "[transition]" in md  # tagged open bug highlighted
    assert (report_dir / "patches" / "foo_1.0-1ubuntu1.patch").exists()
    assert (report_dir / "evidence" / "transcript.jsonl").exists()
    assert (report_dir / "evidence" / "bundle.json").exists()
    assert list((report_dir / "evidence" / "excerpts").glob("build_foo_amd64*"))
    cmds = (report_dir / "commands.sh").read_text()
    assert "exit 0" in cmds and "patch -p1" in cmds
    # the initial bundle carried the KB hint and the failure window
    first_user = llm.calls[0]["messages"][1]["content"]
    assert "py-removed-imp" in first_user and "No module named 'imp'" in first_user


def test_triage_no_llm(env):
    svc, _, _ = env
    report_dir, data = triage(svc, "foo", TriageOptions(no_llm=True))
    r = load_report(report_dir)
    assert r["llm"] == {"ran": False, "reason": "--no-llm", "model": "fake/model"}
    assert r["summary"]["confidence"] == "deterministic only"
    assert "FTBFS" in r["summary"]["root_cause"]


def test_private_ppa_refused(env, workspace):
    svc, lp, _ = env
    workspace.transition.build_location, workspace.transition.ppa = "ppa", "~team/ubuntu/secret"
    lp.private_ppa = True
    llm = ScriptedLLM([])
    report_dir, _ = triage(svc, "foo", TriageOptions(), llm_client=llm)
    r = load_report(report_dir)
    assert llm.calls == [] and "private" in r["llm"]["reason"]
    svc.cfg.allow_private_data = True
    llm = ScriptedLLM([[call("final_report", **minimal_final())]])
    triage(svc, "foo", TriageOptions(), llm_client=llm)
    assert llm.calls


def test_follow_blockers(workspace):
    lp = FakeLP().add("nordugrid-arc", "7.1.2-4", "proposed", builds=[("amd64", OK)]) \
        .add("xrootd", "5.8.0-1", "proposed", builds=[("amd64", OK)])
    http = FakeHTTP({EXCUSES_URL.format(series="tenacious"): (FIX / "update_excuses.yaml.xz").read_bytes()})
    svc = make_services(workspace, cfg(), Progress(quiet=True), lp=lp, http=http)
    report_dir, data = triage(svc, "nordugrid-arc", TriageOptions(no_llm=True))
    r = load_report(report_dir)
    assert r["blockers"]["roots"] == ["xrootd"]
    assert any("th triage xrootd" in s for s in r["summary"]["next_steps"])
    report_dir, data = triage(svc, "nordugrid-arc", TriageOptions(no_llm=True, follow_blockers=True))
    assert data.pkg == "xrootd" and report_dir.parent.name == "xrootd"


def test_retry_url_for_flaky_test(workspace, monkeypatch):
    lp = FakeLP().add("aflplusplus", "5.02c-0.5ubuntu4", "proposed", builds=[("amd64", OK)])
    http = FakeHTTP({EXCUSES_URL.format(series="tenacious"): (FIX / "update_excuses.yaml.xz").read_bytes()})
    svc = make_services(workspace, cfg(), Progress(quiet=True), lp=lp, http=http)
    from transition_helper.autopkgtest import RunResult

    monkeypatch.setattr(svc.autopkgtest, "history", lambda *a, **k: [
        RunResult("r0", 16, "5.02c-0.5ubuntu4", ["aflplusplus/5.02c-0.5ubuntu4"])])
    report_dir, data = triage(svc, "aflplusplus", TriageOptions(no_llm=True))
    cmds = (report_dir / "commands.sh").read_text()
    assert "request.cgi?release=tenacious&arch=amd64&package=aflplusplus" in cmds
    assert "\n# xdg-open" in cmds  # flagged ones are plain (commented-out) commands
    r = load_report(report_dir)
    assert all(t["likely_infra"] for t in r["evidence"]["tests"])


@requires_tools
def test_followup_creates_revision(env):
    svc, _, _ = env
    report_dir, _ = triage(svc, "foo", TriageOptions(), llm_client=ScriptedLLM(ftbfs_script()))
    other = GOOD.replace("+import importlib", "+import importlib.util")
    llm = ScriptedLLM([
        "It fails because of the imp module.",
        [call("propose_patch", target_pkg="foo", unified_diff=other, description="Port, v2",
              patch_name="port-imp")],
        [call("final_report", **minimal_final(confidence="high", fixes=[
            {"target_pkg": "foo", "target_version": "1.0-1build1", "rationale": "own code",
             "patch_ref": "port-imp"}]))],
    ])
    inputs = iter(["why does it fail?", "/report", "/quit"])
    out = []
    rc = followup(svc, report_dir, client=llm, input_fn=lambda _: next(inputs), out=out.append)
    assert rc == 0
    assert "It fails because of the imp module." in out
    assert (report_dir / "report.v2.md").exists() and (report_dir / "report.v2.json").exists()
    assert (report_dir / "patches" / "foo_1.0-1ubuntu1.v2.patch").exists()
    v2 = load_report(report_dir)
    assert v2["revision"] == 2 and v2["summary"]["confidence"] == "high"
    assert "importlib.util" in (report_dir / "patches" / "foo_1.0-1ubuntu1.v2.patch").read_text()
    # the context message carried the previous report
    assert "port-imp" in llm.calls[0]["messages"][1]["content"]


@requires_tools
def test_file_bug_no_then_yes(env):
    svc, lp, _ = env
    report_dir, _ = triage(svc, "foo", TriageOptions(), llm_client=ScriptedLLM(ftbfs_script()))
    out = []
    assert file_bug(svc, report_dir, input_fn=lambda _: "", out=out.append) == 0
    text = "\n".join(out)
    assert "not filed" in text and lp.created == []
    assert "Title:   foo: FTBFS with Python 3.15 (imp removed)" in text
    assert "Tags:    python3.15 ftbfs" in text
    assert "Possible duplicates" in text and "LP: #1" in text

    out = []
    assert file_bug(svc, report_dir, input_fn=lambda _: "y", out=out.append) == 0
    assert lp.created and lp.created[0]["tags"] == ["python3.15", "ftbfs"]
    r = load_report(report_dir)
    assert r["bug"]["id"] == 4242
    patch = (report_dir / r["patches"]["foo"]).read_text()
    assert "(LP: #4242)" in patch and "Bug-Ubuntu: https://bugs.launchpad.net/bugs/4242" in patch
    assert lp.attached and lp.attached[0][1].endswith(".patch") and b"LP: #4242" in lp.attached[0][2]
    assert "LP: #4242" in (report_dir / "report.md").read_text()


@requires_tools
def test_learn(env, tmp_path):
    svc, _, _ = env
    report_dir, _ = triage(svc, "foo", TriageOptions(), llm_client=ScriptedLLM(ftbfs_script()))
    report = load_report(report_dir)
    pats = candidate_patterns(report)
    assert any("No\\ module\\ named" in p or "No module named" in p for p in pats)
    # "editor" that accepts the prefilled draft unchanged
    editor = f"{sys.executable} -c pass"
    out = []
    dest = learn(svc.ws, svc.cfg, report_dir, editor=editor, input_fn=lambda _: "", out=out.append)
    assert dest and dest.parent == svc.ws.kb_dir
    sig = yaml.safe_load(dest.read_text())
    assert sig["scope"] == "python3.15" and sig["applies_to"] == ["build"]
    assert any(line.startswith("  build:foo:") for line in out)
    # the new signature is picked up by the next run
    from transition_helper.kb import KB

    assert KB.load([svc.ws.kb_dir]).get(sig["id"])


def test_learn_rejects_nonmatching(env, tmp_path):
    svc, _, _ = env
    report_dir, _ = triage(svc, "foo", TriageOptions(no_llm=True))
    script = tmp_path / "ed.py"
    script.write_text("import sys, yaml\np = sys.argv[1]\nd = yaml.safe_load(open(p))\n"
                      "d['patterns'] = ['zzz-never-matches']\nopen(p, 'w').write(yaml.safe_dump(d))\n")
    out = []
    dest = learn(svc.ws, svc.cfg, report_dir, editor=f"{sys.executable} {script}",
                 input_fn=lambda _: "n", out=out.append)
    assert dest is None and any("matches none" in o for o in out)
