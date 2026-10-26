"""Concise progress lines on stderr ("locate: ...", "llm[3/20]: ...")."""

from __future__ import annotations

import sys
from typing import TextIO


class Progress:
    def __init__(self, quiet: bool = False, stream: TextIO | None = None):
        self.quiet = quiet
        self.stream = stream or sys.stderr
        self.lines: list[str] = []

    def step(self, tag: str, msg: str) -> None:
        line = f"{tag}: {msg}"
        self.lines.append(line)
        if not self.quiet:
            print(line, file=self.stream, flush=True)

    def warn(self, msg: str) -> None:
        # warnings are shown even with --quiet
        line = f"warning: {msg}"
        self.lines.append(line)
        print(line, file=self.stream, flush=True)


class NullProgress(Progress):
    def __init__(self):
        super().__init__(quiet=True)

    def warn(self, msg: str) -> None:
        self.lines.append(f"warning: {msg}")
