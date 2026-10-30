"""Editable harness overlay: short text files appended to the intake, worker and selection prompts.

The improvement loop edits only these files (sq/overlay/*.md). Missing or empty files add nothing.
"""
from pathlib import Path

DIR = Path(__file__).resolve().parent / "overlay"
NAMES = ("intake", "worker", "audit", "adjudicate")
MAX_CHARS = 2500


def text(name: str) -> str:
    if name not in NAMES:
        raise ValueError(f"unknown overlay {name!r}")
    try:
        body = (DIR / f"{name}.md").read_text().strip()
    except OSError:
        return ""
    return ("\n\n" + body[:MAX_CHARS]) if body else ""
