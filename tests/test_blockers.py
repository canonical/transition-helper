from pathlib import Path

from transition_helper.blockers import blocker_chain
from transition_helper.excuses import ExcusesIndex

FIX = Path(__file__).parent / "fixtures"


def ix(spec: dict[str, list[str]]) -> ExcusesIndex:
    return ExcusesIndex.from_data({"sources": [
        {"source": k, "item-name": k, "new-version": "1", "dependencies": {"blocked-by": v} if v else {}}
        for k, v in spec.items()]})


def test_fixture_chain():
    excuses = ExcusesIndex.from_file(FIX / "update_excuses.yaml.xz")
    chain = blocker_chain(excuses, "nordugrid-arc")
    assert [(e.pkg, e.blocker) for e in chain.edges] == [("nordugrid-arc", "xrootd")]
    assert chain.roots == ["xrootd"] and not chain.cycles


def test_multi_level_and_missing():
    chain = blocker_chain(ix({"a": ["b"], "b": ["c", "gone"], "c": []}), "a")
    assert chain.roots == ["c"] and chain.not_in_proposed == ["gone"]
    assert [e.blocker for e in chain.edges] == ["b", "c", "gone"]


def test_cycle_guard():
    chain = blocker_chain(ix({"a": ["b"], "b": ["c"], "c": ["a"]}), "a")
    assert chain.cycles == [["a", "b", "c", "a"]]
    assert set(chain.roots) == {"a", "b", "c"}
    # cycle plus a real root: the real root wins
    chain = blocker_chain(ix({"a": ["b"], "b": ["a", "r"], "r": []}), "a")
    assert chain.roots == ["r"] and chain.cycles


def test_depth_limit():
    spec = {f"p{i}": [f"p{i+1}"] for i in range(10)} | {"p10": []}
    chain = blocker_chain(ix(spec), "p0", max_depth=3)
    assert chain.truncated and len(chain.edges) == 3


def test_uninstallable_via_binary_lookup():
    excuses = ExcusesIndex.from_file(FIX / "update_excuses.yaml.xz")
    chain = blocker_chain(excuses, "boost-defaults",
                          bin_to_src=lambda b, a: {"libboost-cobalt1.92-dev": "boost1.92"}.get(b))
    assert ("boost-defaults", "boost1.92") in [(e.pkg, e.blocker) for e in chain.edges]
    assert chain.not_in_proposed == ["boost1.92"]
