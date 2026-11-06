"""Loading agentic structured trajectories.

One loader, not one per framework. Verified: the AutoGen, LangGraph and MAF
exports share the same top-level schema (`trace_metadata` + `agents`, with
`step_id` throughout) and differ only in additive metadata keys, because the
upstream OTel capture pipeline already normalises them.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load(path: str | Path) -> tuple[dict, str]:
    """Return (parsed, prompt_text) for a trajectory file.

    The prompt text is the pretty-printed JSON, which is what the annotator
    strategies inject into their prompts.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"No trajectory at {path}")
    parsed = json.loads(path.read_text(encoding="utf-8"))
    text = json.dumps(parsed, indent=2, ensure_ascii=False, default=str)
    return parsed, text


def step_ids(parsed: Any) -> list[str]:
    """Every step_id in the trajectory, in document order, deduplicated.

    Used to check that a model returned annotations for all steps.
    """
    found: list[str] = []
    seen: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            sid = node.get("step_id")
            if sid is not None and str(sid) not in seen:
                seen.add(str(sid))
                found.append(str(sid))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(parsed)
    return found


def step_records(parsed: Any) -> list[dict]:
    """Index original step blocks with their owning agent and JSON Pointer.

    Keep duplicate IDs as separate records: an ID alone may be ambiguous.
    JSON is formatted here so browsers cannot round large integer values.
    """
    found = []
    invocation_count = 0

    def walk(value, path='', agent=None, invocation=None, invocation_path=None):
        nonlocal invocation_count
        if isinstance(value, dict):
            metadata = value.get('agent_metadata')
            if isinstance(metadata, dict):
                invocation_count += 1
                agent = metadata.get('agent_name')
                invocation = invocation_count
                invocation_path = path
            if value.get('step_id') is not None:
                found.append({
                    'step_id': str(value['step_id']), 'agent_name': agent,
                    'invocation': invocation, 'invocation_path': invocation_path,
                    'path': path, 'block_name': path.rsplit('/', 1)[-1],
                    'json': json.dumps(value, ensure_ascii=False, indent=2),
                })
            for key, child in value.items():
                token = str(key).replace('~', '~0').replace('/', '~1')
                walk(child, path + '/' + token, agent, invocation, invocation_path)
        elif isinstance(value, list):
            for i, child in enumerate(value):
                walk(child, path + '/' + str(i), agent, invocation, invocation_path)

    walk(parsed)
    return found
