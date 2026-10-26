from pathlib import Path

import pytest

from transition_helper.excuses import ExcusesError, ExcusesIndex, parse_excuse, strip_html

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def excuses():
    return ExcusesIndex.from_file(FIX / "update_excuses.yaml.xz")


def test_parse_fixture(excuses):
    assert "aflplusplus" in excuses and not excuses.errors
    e = excuses.get("aflplusplus")
    assert e.new_version == "5.02c-0.5ubuntu4"
    fails = e.failing_tests()
    assert {t.arch for t in fails} >= {"amd64", "arm64", "ppc64el"}
    t = fails[0]
    assert t.test_pkg == "aflplusplus" and t.test_version == "5.02c-0.5ubuntu4"
    assert t.triggers == ["aflplusplus/5.02c-0.5ubuntu4"]
    assert t.run_id and t.run_id.endswith("@")


def test_depends_and_blockers(excuses):
    n = excuses.get("nordugrid-arc")
    assert n.blocked_by == ["xrootd"] and n.invalidated_by_other_package
    b = excuses.get("boost-defaults")
    assert b.depends_verdict == "REJECTED_PERMANENTLY"
    assert ("libboost-cobalt1.92-dev", "1.92.0-4ubuntu1", "amd64") in b.impossible_depends
    assert b.component_mismatch
    u = parse_excuse({"source": "x", "excuses": ["libx-dev/i386 has unsatisfiable dependency"]})
    assert u.unsatisfiable == [("libx-dev", "i386")]
    o = excuses.get("opencv")
    assert o.migrate_after == ["vtk9"] and o.missing_builds == ["riscv64"]


def test_block_bugs_and_no_binaries(excuses):
    n = excuses.get("netgen-lvs")
    assert n.block_bugs == ["2170615"] and n.block_bugs_verdict == "REJECTED_PERMANENTLY"
    t = excuses.get("toil")
    assert t.old_version is None and "no-binaries" in t.reasons


def test_tolerant_parsing():
    e = parse_excuse({"source": "x", "policy_info": "garbage", "excuses": ["<b>hi</b> &amp; bye"],
                      "missing-builds": [], "dependencies": None})
    assert e.lines == ["hi & bye"] and e.tests == [] and e.missing_builds == []
    with pytest.raises(ExcusesError):
        parse_excuse({"no": "source"})
    with pytest.raises(ExcusesError):
        ExcusesIndex.from_data({"items": []})
    ix = ExcusesIndex.from_data({"sources": [{"source": "a"}, "junk"]})
    assert "a" in ix and len(ix.errors) == 1


def test_strip_html():
    assert strip_html('<a href="#x">x</a> ♻ ') == "x"


def test_load_excuses_revalidates(tmp_path):
    from datetime import datetime, timedelta, timezone

    from fakes import FakeHTTP
    from transition_helper import excuses as ex
    from transition_helper.cache import Cache

    url = ex.EXCUSES_URL.format(series="tenacious")
    data = (FIX / "update_excuses.yaml.xz").read_bytes()
    t1, t2 = "Mon, 26 Oct 2026 21:08:30 GMT", "Tue, 27 Oct 2026 03:41:02 GMT"
    http = FakeHTTP({url: data, "HEAD " + url: {"last-modified": t1}})
    cache = Cache(tmp_path)
    downloads = lambda: http.requests.count(url)  # noqa: E731

    ix = ex.load_excuses(cache, http, "tenacious")
    assert "aflplusplus" in ix and downloads() == 1
    # same timestamp on the server: HEAD only, no download
    ex.load_excuses(cache, http, "tenacious")
    assert http.requests.count("HEAD " + url) == 2 and downloads() == 1
    # newer file published (at whatever time): downloaded again
    http.routes["HEAD " + url] = {"last-modified": t2}
    ex.load_excuses(cache, http, "tenacious")
    assert downloads() == 2
    # an older timestamp (e.g. a lagging mirror) does not replace the newer copy
    http.routes["HEAD " + url] = {"last-modified": t1}
    ex.load_excuses(cache, http, "tenacious")
    assert downloads() == 2
    # server unreachable: the cached copy is used, with a warning
    del http.routes["HEAD " + url]
    warnings = []
    assert "aflplusplus" in ex.load_excuses(cache, http, "tenacious", warn=warnings.append)
    assert warnings and downloads() == 2
    # --refresh always downloads
    http.routes["HEAD " + url] = {"last-modified": t1}
    ex.load_excuses(Cache(tmp_path, refresh=True), http, "tenacious")
    assert downloads() == 3

    gen = datetime.fromisoformat(ix.generated).replace(tzinfo=timezone.utc)
    assert ix.age_hours(gen + timedelta(hours=2)) == 2.0
