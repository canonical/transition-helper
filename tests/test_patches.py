import subprocess

import pytest

from conftest import requires_tools
from transition_helper.patches import (PatchError, PatchSession, dep3_header, sanitize_patch_name,
                                       split_diff)
from transition_helper.progress import NullProgress
from transition_helper.sources import SourceManager

pytestmark = requires_tools

GOOD = """\
--- a/src/hello.py
+++ b/src/hello.py
@@ -1,5 +1,5 @@
-import sys
+import importlib


 def main():
     print('hello')
"""

BAD = """\
--- a/src/hello.py
+++ b/src/hello.py
@@ -1,3 +1,3 @@
-import os
+import importlib


"""

CONTROL = """\
--- a/debian/control
+++ b/debian/control
@@ -1,3 +1,3 @@
 Source: foo
 Maintainer: Test <test@example.com>
-Build-Depends: debhelper-compat (= 13)
+Build-Depends: debhelper-compat (= 13), python3-setuptools
"""

EXISTING_PATCH = """\
Description: say hi
--- a/src/hello.py
+++ b/src/hello.py
@@ -4,2 +4,2 @@
 def main():
-    print('hello')
+    print('hi')
"""

MAINT = ("Jane Dev", "jane@example.com")


def session(tmp_path, sources):
    return PatchSession(sources, tmp_path / "work", MAINT, "tenacious",
                        "Fix FTBFS with python3-defaults 3.15")


@pytest.fixture
def sources(tmp_path):
    return SourceManager(tmp_path / "cache", None, None, NullProgress())


def unpack(sources, dsc, name="foo", version="1.0-1"):
    return sources.unpack(name, version, dsc)


def test_unpack_creates_git_tree(make_pkg, sources):
    tree = unpack(sources, make_pkg(patches={"01-hi.patch": EXISTING_PATCH}))
    assert tree.is_quilt and (tree.root / ".git").is_dir()
    # patches are applied, and the applied state is committed
    assert "print('hi')" in (tree.root / "src/hello.py").read_text()
    st = subprocess.run(["git", "status", "--porcelain"], cwd=tree.root, capture_output=True, text=True)
    assert st.stdout == ""
    assert sources.existing("foo", "1.0-1").root == tree.root


def test_good_diff_quilt(make_pkg, sources, tmp_path):
    tree = unpack(sources, make_pkg())
    s = session(tmp_path, sources)
    r = s.propose("foo", GOOD, "Port away from sys\n\nLonger text.", "port-imp",
                  target_version="1.0-1")
    assert r.new_version == "1.0-1ubuntu1"
    d = r.combined_diff
    assert "+++ b/debian/patches/port-imp.patch" in d
    assert "+++ b/debian/patches/series" in d and "+port-imp.patch" in d
    assert "+Description: Port away from sys" in d and "+Forwarded: no" in d
    assert "+foo (1.0-1ubuntu1) tenacious; urgency=medium" in d
    assert "+    - d/p/port-imp.patch: Port away from sys." in d
    # upstream file itself is untouched in the combined diff (it lives in the quilt patch)
    assert "\n+++ b/src/hello.py\n" not in d
    # the combined diff applies with patch -p1 to a freshly unpacked tree
    fresh = tmp_path / "fresh"
    subprocess.run(["dpkg-source", "-x", str(next((tmp_path / "pkgsrc").rglob("*.dsc"))), str(fresh)],
                   check=True, capture_output=True)
    subprocess.run(["patch", "-p1", "--dry-run"], cwd=fresh, input=d, text=True, check=True,
                   capture_output=True)
    # the cached tree is left pristine
    assert "import sys" in (tree.root / "src/hello.py").read_text()


def test_bad_diff_rejected(make_pkg, sources, tmp_path):
    unpack(sources, make_pkg())
    s = session(tmp_path, sources)
    with pytest.raises(PatchError, match="git apply --check failed"):
        s.propose("foo", BAD, "nope", "bad", target_version="1.0-1")
    assert s.targets() == []


def test_quilt_series_conflict(make_pkg, sources, tmp_path):
    tree = unpack(sources, make_pkg(patches={"01-hi.patch": EXISTING_PATCH}))
    s = session(tmp_path, sources)
    # editing the existing quilt patch so it no longer applies breaks the series
    broken = (tree.root / "debian/patches/01-hi.patch").read_text().replace("-    print('hello')", "-    print('HELLO')")
    import difflib

    diff = "".join(difflib.unified_diff(
        (tree.root / "debian/patches/01-hi.patch").read_text().splitlines(True), broken.splitlines(True),
        "a/debian/patches/01-hi.patch", "b/debian/patches/01-hi.patch"))
    upstream = "--- a/src/hello.py\n+++ b/src/hello.py\n@@ -1,2 +1,2 @@\n-import sys\n+import importlib\n \n"
    with pytest.raises(PatchError, match="quilt series no longer applies"):
        s.propose("foo", upstream + diff, "touch both", "both", target_version="1.0-1")


def test_two_conflicting_proposals(make_pkg, sources, tmp_path):
    unpack(sources, make_pkg())
    s = session(tmp_path, sources)
    s.propose("foo", GOOD, "first", "first", target_version="1.0-1")
    other = GOOD.replace("+import importlib", "+import importlib.util")
    with pytest.raises(PatchError, match="quilt series no longer applies"):
        s.propose("foo", other, "second", "second", target_version="1.0-1")
    # replacing the first one under the same name works
    r = s.propose("foo", other, "first, revised", "first", target_version="1.0-1")
    assert [p.patch_name for p in r.proposals] == ["first.patch"]
    assert "importlib.util" in r.combined_diff


def test_packaging_only_and_bug(make_pkg, sources, tmp_path):
    unpack(sources, make_pkg())
    s = session(tmp_path, sources)
    r = s.propose("foo", CONTROL, "Build-depend on setuptools", "bd-setuptools", bug=99,
                  target_version="1.0-1")
    assert r.proposals[0].quilt_patch is None
    assert "+++ b/debian/control" in r.combined_diff
    assert "debian/patches" not in r.combined_diff
    assert "(LP: #99)" in r.combined_diff and "d/control: Build-depend on setuptools." in r.combined_diff
    # rebuild with a different bug number (file-bug path)
    again = s.rebuild_all(bug=1234)[0]
    assert "(LP: #1234)" in again.combined_diff


def test_native_direct_edit(make_pkg, sources, tmp_path):
    dsc = make_pkg(name="nat", version="2.0", fmt="3.0 (native)")
    tree = sources.unpack("nat", "2.0", dsc)
    assert not tree.is_quilt
    s = session(tmp_path, sources)
    r = s.propose("nat", GOOD, "Port", "port", target_version="2.0")
    assert r.new_version == "2.0ubuntu1"
    assert "+++ b/src/hello.py" in r.combined_diff and "debian/patches" not in r.combined_diff


def test_quilt_dep3_with_bug(make_pkg, sources, tmp_path):
    unpack(sources, make_pkg())
    s = session(tmp_path, sources)
    r = s.propose("foo", GOOD, "Port", "port", bug=7, target_version="1.0-1")
    assert "+Bug-Ubuntu: https://bugs.launchpad.net/bugs/7" in r.combined_diff


def test_split_diff_validation():
    with pytest.raises(PatchError, match="escapes"):
        split_diff("--- a/../x\n+++ b/../x\n@@ -1 +1 @@\n-a\n+b\n")
    with pytest.raises(PatchError, match="a/ and b/"):
        split_diff("--- x\n+++ x\n@@ -1 +1 @@\n-a\n+b\n")
    with pytest.raises(PatchError, match="generates"):
        split_diff("--- a/debian/changelog\n+++ b/debian/changelog\n@@ -1 +1 @@\n-a\n+b\n")
    with pytest.raises(PatchError, match="not editable"):
        split_diff("--- a/.pc/x\n+++ b/.pc/x\n@@ -1 +1 @@\n-a\n+b\n")
    with pytest.raises(PatchError):
        split_diff("just prose")
    parts = split_diff("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
                       "diff --git a/debian/y b/debian/y\nnew file mode 100644\n--- /dev/null\n+++ b/debian/y\n@@ -0,0 +1 @@\n+c")
    assert [(p.path, p.is_debian) for p in parts] == [("x", False), ("debian/y", True)]


def test_patch_name_and_header():
    assert sanitize_patch_name("fix imp") == "fix-imp.patch"
    assert sanitize_patch_name("debian/patches/x.diff") == "x.diff"
    with pytest.raises(PatchError):
        sanitize_patch_name("../evil")
    h = dep3_header("Line one\n\nmore", ("A", "a@b"), 5)
    assert h.startswith("Description: Line one\n .\n more\nAuthor: A <a@b>\nBug-Ubuntu:")
    assert h.endswith("---\n")
