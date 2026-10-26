import shutil
from pathlib import Path

import pytest

from transition_helper.kb import KB, validate_signature
from transition_helper.logs import (LogError, LogStore, dedupe, failure_signature,
                                    failure_windows, normalise)

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def store(tmp_path):
    s = LogStore(tmp_path / "logs")
    s.add_text("build:foo:amd64", (FIX / "build_foo_amd64.log").read_text())
    shutil.copy(FIX / "autopkgtest_netgen-lvs_amd64.log.gz",
                s._file("test:netgen-lvs:amd64:20261023_143710_2b2dd@"))
    s._write_meta("test:netgen-lvs:amd64:20261023_143710_2b2dd@", "fixture", None)
    return s


def test_store_reads(store):
    meta = store.meta("build:foo:amd64")
    assert meta.lines == len((FIX / "build_foo_amd64.log").read_text().splitlines())
    assert store.read("build:foo:amd64", 1, 2)[0] == (1, (FIX / "build_foo_amd64.log").read_text().splitlines()[0])
    tail = store.tail("build:foo:amd64", 3)
    assert [n for n, _ in tail] == [meta.lines - 2, meta.lines - 1, meta.lines]
    with pytest.raises(LogError):
        store.read("build:../etc:amd64", 1, 2)
    with pytest.raises(LogError):
        list(store.iter_lines("build:nope:amd64"))
    # stored gzipped
    assert store._file("build:foo:amd64").read_bytes()[:2] == b"\x1f\x8b"


def test_grep(store):
    groups, total = store.grep("build:foo:amd64", r"No module named", context=1)
    assert total == 1 and len(groups) == 1
    assert any("No module named 'imp'" in line for _, line in groups[0])
    assert len(groups[0]) == 3
    groups, total = store.grep("build:foo:amd64", r"pybuild", context=0, max_matches=2)
    assert total > 2 and sum(len(g) for g in groups) == 2
    with pytest.raises(LogError):
        store.grep("build:foo:amd64", "(")


def test_failure_windows_build(store):
    ws = failure_windows(store, "build:foo:amd64")
    assert ws
    text = "\n".join(w.render() for w in ws)
    assert "ModuleNotFoundError: No module named 'imp'" in text
    assert "dpkg-buildpackage: error" in text
    # windows are disjoint and sorted
    assert all(a.end < b.start for a, b in zip(ws, ws[1:]))


def test_failure_windows_autopkgtest(store):
    ws = failure_windows(store, "test:netgen-lvs:amd64:20261023_143710_2b2dd@")
    text = "\n".join(w.render() for w in ws)
    assert "can-show-help        FAIL stderr" in text


def test_normalise_and_dedupe(store):
    a = normalise("  47s /build/foo-abc/foo-1.0/x.c:12: error at 0x7ffd1234 on x86_64")
    b = normalise("  51s /build/foo-xyz/foo-1.0/x.c:99: error at 0x7ffe0000 on aarch64")
    assert a == b
    groups = dedupe({
        "test:b:arm64:r1": ("sig1", []),
        "test:b:amd64:r2": ("sig1", []),
        "test:b:s390x:r3": ("sig2", []),
        "test:b:ppc64el:r4": ("other", ["kb-x"]),
        "test:b:armhf:r5": ("different", ["kb-x"]),
    })
    by_rep = {g.representative: g for g in groups}
    assert set(by_rep) == {"test:b:amd64:r2", "test:b:s390x:r3", "test:b:ppc64el:r4"}
    assert len(by_rep["test:b:ppc64el:r4"].members) == 2


def test_signature_stable_across_arches(tmp_path):
    s = LogStore(tmp_path)
    s.add_text("test:b:amd64:r1", "  10s x\n  11s E   AssertionError: x86_64 0x1 != 2\n")
    s.add_text("test:b:arm64:r2", "  20s x\n  21s E   AssertionError: aarch64 0x9 != 2\n")
    sa = failure_signature(failure_windows(s, "test:b:amd64:r1"))
    sb = failure_signature(failure_windows(s, "test:b:arm64:r2"))
    assert sa == sb


def test_kb_seed_matches(store):
    kb = KB.load([])
    assert not kb.errors
    ms = kb.match_log(store, "build:foo:amd64")
    assert "py-removed-imp" in [m.sig_id for m in ms]
    ms = kb.match_log(store, "test:netgen-lvs:amd64:20261023_143710_2b2dd@")
    assert [m.sig_id for m in ms] == ["autopkgtest-stderr-only"]
    # applies_to is honoured: build-only signatures don't match test logs
    assert not [m for m in ms if m.sig_id == "py-setup-py-test"]


def test_kb_override_and_errors(tmp_path):
    g, w = tmp_path / "global", tmp_path / "ws"
    g.mkdir(), w.mkdir()
    (g / "a.yaml").write_text("id: sig-a\nscope: global\napplies_to: [build]\npatterns: ['foo']\n"
                              "explanation: global\n")
    (w / "a.yaml").write_text("id: sig-a\nscope: py\napplies_to: [build]\npatterns: ['foo']\n"
                              "explanation: workspace\n")
    (w / "bad.yaml").write_text("id: Bad Id\npatterns: ['(']\n")
    kb = KB.load([g, w], include_seed=False)
    assert kb.get("sig-a").explanation == "workspace"
    assert len(kb.errors) == 1 and "bad.yaml" in kb.errors[0]
    assert kb.lookup("sig-a")[0].id == "sig-a"
    assert kb.lookup("workspace")[0].id == "sig-a"


def test_validate_signature():
    assert validate_signature({"id": "x", "scope": "g", "applies_to": ["build"], "patterns": ["a"]}) == []
    errs = validate_signature({"id": "x", "scope": "g", "applies_to": ["nope"], "patterns": ["("],
                               "extra": 1})
    assert len(errs) == 3
