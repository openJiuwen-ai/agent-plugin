"""Validation and preparation shared by preview and Run evaluation."""
import json
import math
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from .core.config import DEFAULT_PARAMETERS, evaluation_defaults
from .core.providers import PROVIDERS, sampling_params
from .domain.trajectories import step_ids

MAX_BODY = 10 * 1024 * 1024


def trajectory_summary(value):
    if not isinstance(value, dict) or "annotations" in value:
        raise ValueError("Select an agent trajectory, not an evaluation annotations file")
    metadata = value.get("agent_metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("agent_metadata must be a JSON object when provided")
    if isinstance(value.get("agents"), (list, dict)) and value.get("trace_metadata") is not None:
        kind = "Agent workflow"
    elif isinstance(value.get("agent_metadata"), dict):
        kind = "OpenJiuwen agent trace"
    elif isinstance(value.get("steps"), list):
        kind = "Step trajectory"
    else:
        raise ValueError("Unrecognized trajectory format: expected agent_metadata, trace_metadata + agents, or steps")
    ids = step_ids(value)
    if not ids:
        raise ValueError("The trajectory contains no step_id entries")
    return {"format": kind, "step_count": len(ids), "agent_id": metadata.get("agent_id"),
            "agent_name": metadata.get("agent_name") or metadata.get("name")}


def check_output(directory):
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError("Evaluation output directory must not be a symbolic link")
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryFile(dir=directory):
            pass
    except OSError:
        raise ValueError("Cannot write results beside this input. Choose a writable server folder or upload the file.") from None
    return directory


def validate_request(value):
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    allowed = {"client", "api_key", "model", "base_url", "strategy", "taxonomy",
               "dedup", "temperature", "top_p", "trajectory", "output_subdir"}
    if set(value) - allowed:
        raise ValueError("Unknown evaluation parameters")
    client = value.get("client", DEFAULT_PARAMETERS["client"])
    if not isinstance(client, str) or client not in PROVIDERS:
        raise ValueError("Invalid client")
    result = evaluation_defaults(client)
    result.update(value)
    for key, choices in (
        ("client", PROVIDERS),
        ("strategy", ("taxonomy-guided", "zero-shot")),
        ("taxonomy", ("ours", "mast")),
        ("dedup", ("none", "exact", "by_code")),
    ):
        if not isinstance(result[key], str) or result[key] not in choices:
            raise ValueError(f"Invalid {key}")
    for key in ("api_key", "model"):
        if not isinstance(result.get(key), str) or not result[key].strip() or len(result[key]) > 8192:
            raise ValueError(f"{key} is required")
    trajectory = value.get("trajectory")
    trajectory_summary(trajectory)
    if not isinstance(result["output_subdir"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", result["output_subdir"]):
        raise ValueError("Results subfolder must contain only letters, digits, _ or -")
    base_url = value.get("base_url") or PROVIDERS[result["client"]]["base_url"]
    if not isinstance(base_url, str):
        raise ValueError("Invalid base URL")
    url = urlsplit(base_url)
    if url.scheme not in ("https", "http") or not url.hostname or url.username or url.password or url.fragment or url.query:
        raise ValueError("Base URL must be an http(s) API endpoint without credentials or query")
    result["base_url"] = base_url
    for key, upper in (("temperature", 2), ("top_p", 1)):
        number = result[key]
        if number is not None and (type(number) not in (int, float) or not math.isfinite(number) or not 0 <= number <= upper):
            raise ValueError(f"{key} must be between 0 and {upper}")
        result[key] = number
    result.pop("output_subdir", None)
    if result["strategy"] == "zero-shot":
        result["taxonomy"], result["dedup"] = None, "none"
    applied = sampling_params(result["model"].split("/")[-1], result["temperature"], result["top_p"])
    result["temperature"], result["top_p"] = applied.get("temperature"), applied.get("top_p")
    return result
