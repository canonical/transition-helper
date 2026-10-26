from datetime import datetime, timezone

import pytest

from transition_helper.versions import (changelog_stanza, newest, next_ubuntu_version,
                                        strip_ppa)


@pytest.mark.parametrize("version,expected", [
    ("1.2-3", "1.2-3ubuntu1"),
    ("1.2-3build1", "1.2-3ubuntu1"),
    ("1.2-3build12", "1.2-3ubuntu1"),
    ("1.2-3ubuntu1", "1.2-3ubuntu2"),
    ("1.2-3ubuntu9", "1.2-3ubuntu10"),
    ("1.2-3ubuntu2build1", "1.2-3ubuntu3"),
    ("1.2-3ubuntu1~ppa2", "1.2-3ubuntu2"),
    ("1.2-3build1~ppa1", "1.2-3ubuntu1"),
    ("2:1.2-3", "2:1.2-3ubuntu1"),
    ("1.5", "1.5ubuntu1"),
    ("1.5build1", "1.5ubuntu1"),
    ("1.5ubuntu3", "1.5ubuntu4"),
    ("1.2-3ubuntu1.1", "1.2-3ubuntu2"),
])
def test_next_ubuntu_version(version, expected):
    assert next_ubuntu_version(version) == expected


def test_strip_ppa_and_newest():
    assert strip_ppa("1.0-1~ppa3") == "1.0-1"
    assert strip_ppa("1.0-1") == "1.0-1"
    assert newest(["1.0-1", "1.0-2~ppa1", "1.0-1build1"]) == "1.0-2~ppa1"
    assert newest([]) is None


def test_changelog_stanza():
    when = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
    s = changelog_stanza("foo", "1.0-1ubuntu1", "tenacious",
                         ["Fix FTBFS with python3-defaults 3.15\n- d/p/fix-imp.patch: port to importlib."],
                         ("Jane Dev", "jane@example.com"), bug=123, when=when)
    assert s.splitlines()[0] == "foo (1.0-1ubuntu1) tenacious; urgency=medium"
    assert "  * Fix FTBFS with python3-defaults 3.15 (LP: #123)" in s
    assert "    - d/p/fix-imp.patch: port to importlib." in s
    assert s.rstrip().endswith(" -- Jane Dev <jane@example.com>  Mon, 26 Oct 2026 12:00:00 +0000")
    # parseable by python-debian
    from debian.changelog import Changelog

    cl = Changelog(s)
    assert str(cl.version) == "1.0-1ubuntu1" and cl.distributions == "tenacious"
