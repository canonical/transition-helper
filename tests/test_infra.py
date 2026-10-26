from urllib.parse import parse_qs, urlparse

from transition_helper.autopkgtest import RunResult, outcome, retry_url
from transition_helper.infra import assess_build, assess_test, intermittent


def runs(*spec):
    out = []
    for i, (code, trig) in enumerate(spec):
        out.append(RunResult(run_id=f"r{i}", exitcode=code, version="1",
                             triggers=[trig] if trig else []))
    return out


def test_outcomes():
    assert [outcome(c) for c in (0, 2, 8, 4, 6, 16, 20, 12, None)] == [
        "pass", "pass", "pass", "fail", "fail", "tmpfail", "tmpfail", "badpkg", "unknown"]


def test_tmpfail_is_infra():
    v = assess_test("REGRESSION", "amd64", [], runs((16, "a/2")), {"amd64"}, {"amd64", "arm64"})
    assert v.likely_infra and "tmpfail" in v.reasons[0]


def test_log_markers():
    lines = ["E: Failed to fetch http://ftpmaster.internal/x  Temporary failure resolving 'ftpmaster'"]
    v = assess_test("REGRESSION", "amd64", lines, [], {"amd64"}, {"amd64", "arm64"})
    assert v.likely_infra and "network" in v.markers
    assert any("fails only on amd64 and looks like infra" in r for r in v.reasons)


def test_baseline_fails():
    hist = runs((4, "a/2"), (4, "migration-reference/0"), (0, "a/1"))
    v = assess_test("REGRESSION", "amd64", ["FAIL"], hist, {"amd64", "arm64"}, {"amd64", "arm64"})
    assert v.baseline_fails and v.likely_flaky and not v.likely_infra


def test_intermittent():
    assert intermittent(["fail", "pass", "fail", "pass"])
    assert not intermittent(["fail", "fail", "pass", "pass"])
    assert not intermittent(["fail", "tmpfail", "fail"])
    hist = runs((4, "a/2"), (0, "a/2"), (4, "a/2"), (0, "a/2"))
    v = assess_test("REGRESSION", "amd64", [], hist, {"amd64"}, {"amd64"})
    assert v.likely_flaky


def test_real_regression_not_flagged():
    hist = runs((4, "a/2"), (4, "a/2"), (0, "a/1"), (0, None))
    v = assess_test("REGRESSION", "amd64", ["AssertionError: 1 != 2"], hist,
                    {"amd64", "arm64"}, {"amd64", "arm64"})
    assert not v.flagged and v.baseline_fails is False


def test_all_proposed_hint():
    lines = ["The following packages have unmet dependencies:",
             " python3-b : Depends: python3-c (>= 2) but 1 is to be installed"]
    v = assess_test("REGRESSION", "amd64", lines, [], {"amd64"}, {"amd64"})
    assert v.needs_all_proposed
    v = assess_test("REGRESSION", "amd64", [], [], {"amd64"}, {"amd64"}, trigger_count=1,
                    has_unmigrated_deps=True)
    assert v.needs_all_proposed


def test_retry_url():
    url = retry_url("tenacious", "arm64", "libfoo", ["a/2-1", "b/1"], all_proposed=True)
    q = parse_qs(urlparse(url).query)
    assert q == {"release": ["tenacious"], "arch": ["arm64"], "package": ["libfoo"],
                 "trigger": ["a/2-1", "b/1"], "all-proposed": ["1"]}
    url = retry_url("tenacious", "amd64", "x", ["x/1"], ppa="~team/ubuntu/py315")
    assert parse_qs(urlparse(url).query)["ppa"] == ["team/py315"]


def test_build_infra():
    v = assess_build(["dpkg-deb: error: No space left on device"], {"amd64"}, {"amd64", "arm64"})
    assert v.likely_infra and "disk" in v.markers
    assert not assess_build(["error: implicit declaration of function"], {"amd64"}, {"amd64"}).flagged
