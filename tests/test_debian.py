import json
from pathlib import Path

from fakes import FakeHTTP
from transition_helper.cache import Cache
from transition_helper.debian_delta import (CHANGELOG_URL, MADISON_URL, changelog_delta,
                                            debian_delta, parse_madison)

FIX = Path(__file__).parent / "fixtures"


def test_parse_madison_real_fixture():
    data = json.loads((FIX / "madison_python-hypothesis.json").read_text())
    versions, comps = parse_madison(data, "python-hypothesis")
    assert versions == {"unstable": "6.155.7-1"} and comps["unstable"] == "main"


def test_parse_madison_ignores_binnmus():
    data = [{"foo": {"unstable": {
        "1.0-1": {"component": "main", "architectures": ["source", "amd64"], "source": "foo", "source_version": "1.0-1"},
        "1.0-1+b2": {"component": "main", "architectures": ["arm64"], "source": "foo", "source_version": "1.0-1"},
    }, "experimental": {"2.0-1": {"component": "contrib", "source": "foo", "source_version": "2.0-1"}}}}]
    versions, comps = parse_madison(data, "foo")
    assert versions == {"unstable": "1.0-1", "experimental": "2.0-1"} and comps["experimental"] == "contrib"


def test_changelog_delta():
    text = (FIX / "debian_changelog_foo.txt").read_text()
    entries = changelog_delta(text, "1.1-1ubuntu2", ["python3.15", "3.15"])
    assert [e.version for e in entries] == ["1.2-1", "1.1-2"]
    assert entries[0].relevant and not entries[1].relevant
    assert changelog_delta(text, "1.2-1", ["x"]) == []
    assert changelog_delta(text, "1.2-1~ppa1", ["x"]) == []


def test_debian_delta_end_to_end(tmp_path):
    http = FakeHTTP({
        MADISON_URL: [{"foo": {"unstable": {"1.2-1": {"component": "main", "source": "foo", "source_version": "1.2-1"}}}}],
        CHANGELOG_URL.format(component="main", prefix="f", pkg="foo", suite="unstable"):
            (FIX / "debian_changelog_foo.txt").read_text(),
    })
    d = debian_delta(http, Cache(tmp_path), "foo", "1.1-1ubuntu1", ["python3.15", "3.15"])
    assert d.newer_suite == "unstable" and "sync or merge may fix this" in d.hint
    assert "1 changelog entry mention" in d.hint
    d = debian_delta(http, Cache(tmp_path), "foo", "1.2-1ubuntu1", [])
    assert d.newer_suite is None and d.hint is None


def test_debian_delta_not_in_debian(tmp_path):
    d = debian_delta(FakeHTTP({MADISON_URL: []}), Cache(tmp_path), "ubuntu-only", "1.0", [])
    assert d.versions == {} and d.summary() == "not in Debian unstable/experimental"
