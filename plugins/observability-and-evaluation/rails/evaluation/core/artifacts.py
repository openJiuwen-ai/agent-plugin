"""Run directory layout.

Replaces `logs/annotator_<n>/<timestamp>/`, which encoded only the strategy —
two runs of the same strategy with different models, taxonomies, trajectories or
temperatures were indistinguishable by path. The cell key records all of it.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from itertools import count


FAILURE_DETECTOR_RESULT = "failure_detector_result.json"
EVALUATION_DIRECTORY_FORMAT = "%Y-%m-%d_%H-%M-%S"


def create_evaluation_directory(parent: Path, origin: str) -> Path:
    """Create a UTC start-time folder, numbering same-second runs atomically."""
    if origin not in {"terminal", "ui"}:
        raise ValueError("Evaluation origin must be terminal or ui")
    stamp = datetime.now(timezone.utc).strftime(EVALUATION_DIRECTORY_FORMAT)
    parent = Path(parent).expanduser().resolve()
    parent.mkdir(parents=True, exist_ok=True)
    for ordinal in count(1):
        suffix = "" if ordinal == 1 else f"_{ordinal}"
        destination = parent / f"{stamp}_{origin}{suffix}"
        try:
            destination.mkdir(mode=0o700)
        except FileExistsError:
            continue
        return destination


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip("-")


class RunDir:
    """One invocation's artifacts, keyed by every axis that varies."""

    def __init__(self, root: Path, module: str, strategy: str, cell: dict):
        stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        self.root = Path(root) / stamp
        self.cell_key = ".".join(_slug(v) for v in cell.values() if v)
        self.cell = self.root / module / _slug(strategy) / self.cell_key
        self.prompts = self.root / "prompts"
        for d in (self.cell, self.prompts, self.cell / "raw"):
            d.mkdir(parents=True, exist_ok=True)

    def write_prompt(self, name: str, text: str) -> Path:
        """Full rendered prompts land here once — never in the tee'd terminal log."""
        path = self.prompts / f"{_slug(name)}.txt"
        path.write_text(text, encoding="utf-8")
        return path

    def write_raw(self, name: str, text: str) -> Path:
        path = self.cell / "raw" / f"{_slug(name)}.json"
        path.write_text(text, encoding="utf-8")
        return path

    def write_json(self, name: str, payload) -> Path:
        path = self.cell / name
        if is_dataclass(payload):
            payload = asdict(payload)
        path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        return path

    def write_text(self, name: str, text: str) -> Path:
        path = self.cell / name
        path.write_text(text, encoding="utf-8")
        return path

    @property
    def terminal_log(self) -> Path:
        return self.cell / "run.log"
