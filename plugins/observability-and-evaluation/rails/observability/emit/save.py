"""Artifact writing: schema_version stamping happens here, at save time,
so the extractors stay pure functions of their inputs."""
import json
from pathlib import Path

from ..model.version import SCHEMA_VERSION


def save_single_agent_json(agent_json: dict, output_path: str) -> str:
    """Write one agent's JSON to disk immediately after that agent finishes.

   Creates parent directories as needed and dumps with indent=2 and
   default=str (so stray non-serializable values stringify). Returns the
   resolved absolute path. Writing per-agent means a crash mid-workflow still
   leaves the completed agents' JSON on disk.
   """
    output_file = Path(output_path).expanduser().resolve()
    output_file.parent.mkdir(parents=True, exist_ok=True)

    # SCHEMA_VERSION is stamped at SAVE time, not in the extractor: the
    # extractor stays a pure function of its captured inputs (which is what
    # makes the golden replay fixtures stable), while every artifact on disk
    # says which schema it speaks. First key, so vintage is visible at a glance.
    stamped = {"schema_version": SCHEMA_VERSION, **agent_json}

    with output_file.open("w", encoding="utf-8") as f:
        json.dump(stamped, f, ensure_ascii=False, indent=2, default=str)

    return str(output_file)
