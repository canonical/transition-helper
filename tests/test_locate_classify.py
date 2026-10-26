from pathlib import Path

import pytest

from fakes import FakeLP
from transition_helper.classify import Category, Phase, classify, classify_migration
from transition_helper.excuses import ExcusesIndex, parse_excuse
from transition_helper.locate import State, locate

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def excuses():
    return ExcusesIndex.from_file(FIX / "update_excuses.yaml.xz")


OK = "Successfully built"


def run(lp, transition, pkg="foo", excuses=None, arch=None):
    loc = locate(lp, transition, pkg, excuses, arch=arch)
    return loc, classify(loc, excuses.get(pkg) if excuses else None,
                         set(transition.ignored_arches), arch)


def test_build_failed(transition):
    lp = FakeLP().add("foo", "1.0-1", "release").add(
        "foo", "1.0-1build1", "proposed",
        builds=[("amd64", "Failed to build"), ("arm64", "Failed to build"), ("s390x", OK)])
    loc, cls = run(lp, transition)
    assert loc.state == State.BUILD_FAILED and loc.failed_arches == ["amd64", "arm64"]
    assert loc.target_version == "1.0-1build1" and loc.target_location == "proposed"
    assert cls.phase == Phase.BUILD and cls.categories == [Category.FTBFS]
    assert {b.arch for b in cls.build_issues} == {"amd64", "arm64"}


def test_depwait_and_infra(transition):
    lp = FakeLP().add("foo", "1.0-1build1", "proposed", builds=[
        ("amd64", "Dependency wait", "python3-bar (>= 2)"), ("arm64", "Chroot problem"), ("s390x", OK)])
    loc, cls = run(lp, transition)
    assert loc.state == State.BUILD_FAILED
    assert cls.categories == [Category.DEPWAIT, Category.BUILD_INFRA]
    dw = next(b for b in cls.build_issues if b.category == Category.DEPWAIT)
    assert dw.missing_dependency == "python3-bar (>= 2)"


def test_pending_and_partial(transition):
    lp = FakeLP().add("foo", "1.0-1build1", "proposed",
                      builds=[("amd64", "Needs building"), ("arm64", "Currently building")])
    loc, cls = run(lp, transition)
    assert loc.state == State.BUILD_PENDING and cls.categories == [Category.BUILD_PENDING]
    lp = FakeLP().add("foo", "1.0-1build1", "proposed",
                      builds=[("amd64", OK), ("arm64", "Currently building")])
    loc, _ = run(lp, transition)
    assert loc.state == State.PARTIAL_BUILD


def test_ignored_arch_and_focus(transition):
    lp = FakeLP().add("foo", "1.0-1build1", "proposed",
                      builds=[("amd64", OK), ("riscv64", "Failed to build")])
    loc, _ = run(lp, transition)
    assert loc.state == State.IN_PROPOSED_NO_EXCUSE_YET
    assert loc.ignored_failures == ["riscv64"]
    loc, cls = run(lp, transition, arch="riscv64")
    assert loc.state == State.BUILD_FAILED and cls.categories == [Category.FTBFS]


def test_superseded_build_ignored(transition):
    lp = FakeLP().add("foo", "1.0-1build1", "proposed",
                      builds=[("amd64", OK), ("arm64", "Build for superseded Source")])
    loc, _ = run(lp, transition)
    assert loc.state == State.IN_PROPOSED_NO_EXCUSE_YET


def test_ppa_states(transition):
    t = transition
    t.build_location, t.ppa = "ppa", "~team/ubuntu/py315"
    lp = FakeLP().add("foo", "1.0-1", "release").add(
        "foo", "1.0-1build1~ppa1", "ppa", builds=[("amd64", OK), ("arm64", OK)])
    loc, cls = run(lp, t)
    assert loc.state == State.BUILT_IN_PPA_NOT_IN_PROPOSED
    assert "all arches built in PPA; not in -proposed yet" in loc.notes
    assert cls.categories == [Category.NONE]
    # same base version already in -proposed: proposed wins (further along)
    lp.add("foo", "1.0-1build1", "proposed", builds=[("amd64", OK)])
    loc, _ = run(lp, t)
    assert loc.target_location == "proposed"
    lp = FakeLP().add("foo", "1.0-1build1~ppa1", "ppa", builds=[("amd64", "Failed to build")])
    loc, _ = run(lp, t)
    assert loc.state == State.BUILD_FAILED and loc.target_location == "ppa"


def test_migrated_vs_not_rebuilt(transition):
    lp = FakeLP().add("foo", "1.0-1build1", "release")
    loc, cls = run(lp, transition)
    assert loc.state == State.NOT_REBUILT and cls.phase == Phase.NOT_STARTED
    lp.proposed_history.add(("foo", "1.0-1build1"))
    loc, cls = run(lp, transition)
    assert loc.state == State.MIGRATED and cls.phase == Phase.DONE


def test_not_found(transition):
    loc, cls = run(FakeLP(), transition)
    assert loc.state == State.NOT_FOUND and cls.phase == Phase.UNKNOWN


def test_with_excuse_from_fixture(transition, excuses):
    lp = FakeLP().add("aflplusplus", "4.33c-1.1ubuntu1", "release").add(
        "aflplusplus", "5.02c-0.5ubuntu4", "proposed", builds=[("amd64", OK), ("arm64", OK)])
    loc, cls = run(lp, transition, "aflplusplus", excuses)
    assert loc.state == State.IN_PROPOSED_WITH_EXCUSE
    assert cls.primary == Category.AUTOPKGTEST_REGRESSION
    assert all(ft.own for ft in cls.failing_tests)
    assert cls.failing_tests[0].kind == "own test"


def test_excuse_for_other_version(transition, excuses):
    lp = FakeLP().add("aflplusplus", "5.02c-0.5ubuntu5", "proposed", builds=[("amd64", OK)])
    loc, _ = run(lp, transition, "aflplusplus", excuses)
    assert loc.state == State.IN_PROPOSED_NO_EXCUSE_YET
    assert any("excuse is for 5.02c-0.5ubuntu4" in n for n in loc.notes)


@pytest.mark.parametrize("pkg,expected", [
    ("aflplusplus", [Category.AUTOPKGTEST_REGRESSION]),
    ("nordugrid-arc", [Category.DEPENDS_ON_UNMIGRATED]),
    ("boost-defaults", [Category.UNINSTALLABLE, Category.AUTOPKGTEST_PENDING]),
    ("netgen-lvs", [Category.AUTOPKGTEST_REGRESSION, Category.BLOCKED]),
    ("toil", [Category.MISSING_BUILD, Category.AUTOPKGTEST_PENDING]),
])
def test_migration_categories(excuses, pkg, expected):
    cls = classify_migration(excuses.get(pkg))
    for cat in expected:
        assert cat in cls.categories, cls.categories
    assert cls.categories[0] == expected[0]


def test_opencv_ignored_missing_build(excuses):
    # riscv64 is the only missing build; ignoring it drops MISSING_BUILD
    cls = classify_migration(excuses.get("opencv"), ignored_arches={"riscv64"})
    assert Category.MISSING_BUILD not in cls.categories
    assert Category.DEPENDS_ON_UNMIGRATED in cls.categories and cls.blockers == ["vtk9"]
    cls = classify_migration(excuses.get("opencv"))
    assert Category.MISSING_BUILD in cls.categories


def test_reverse_dependency_test_and_triggers():
    e = parse_excuse({
        "source": "a", "item-name": "a", "new-version": "2-1", "old-version": "1-1",
        "migration-policy-verdict": "REJECTED_PERMANENTLY", "reason": ["autopkgtest"],
        "policy_info": {"autopkgtest": {"verdict": "REJECTED_PERMANENTLY", "b/4.5-1": {
            "amd64": ["REGRESSION", "https://autopkgtest.ubuntu.com/results/autopkgtest-tenacious/tenacious/amd64/b/b/20260101_000000_abcde@/log.gz",
                      "https://autopkgtest.ubuntu.com/packages/b/b/tenacious/amd64", None,
                      "https://autopkgtest.ubuntu.com/request.cgi?release=tenacious&arch=amd64&package=b&trigger=a%2F2-1&trigger=c%2F3-1"]}}}})
    cls = classify_migration(e)
    ft = cls.failing_tests[0]
    assert not ft.own and ft.kind == "reverse-dependency test"
    assert ft.triggers == ["a/2-1", "c/3-1"] and ft.test_version == "4.5-1"
    assert ft.run_id == "20260101_000000_abcde@"


def test_valid_candidate_is_none():
    e = parse_excuse({"source": "a", "item-name": "a", "new-version": "2", "is-candidate": True,
                      "migration-policy-verdict": "PASS", "policy_info": {}})
    assert classify_migration(e).categories == [Category.NONE]
