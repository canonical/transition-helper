"""Open text in the user's $VISUAL/$EDITOR and return the result."""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path


class EditorError(Exception):
    pass


def edit(text: str, editor: str | None = None, suffix: str = ".yaml") -> str:
    editor = editor or os.environ.get("VISUAL") or os.environ.get("EDITOR") or "editor"
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False) as f:
        f.write(text)
        path = f.name
    try:
        subprocess.run([*shlex.split(editor), path], check=True)
        return Path(path).read_text()
    except (OSError, subprocess.CalledProcessError) as e:
        raise EditorError(f"editor {editor!r} failed: {e}") from e
    finally:
        os.unlink(path)
