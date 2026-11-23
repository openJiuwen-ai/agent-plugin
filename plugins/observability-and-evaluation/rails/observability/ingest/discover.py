"""Finding and loading artifacts back off disk (ingest side, §3.4)."""
import json
import re
from pathlib import Path


def load_OrchestratorObj_json_files(session_dir: str, run_id: int | str) -> list[tuple[int, str, dict]]:
    """
   Load all OrchestratorObj_*.json files from session directory.
   """
    session_path = Path(session_dir).resolve()
    OrchestratorObj_files = sorted(session_path.glob(f"OrchestratorObj_*_{run_id}.json"))

    results = []

    for file_path in OrchestratorObj_files:
        filename = file_path.name
        #pattern = r'OrchestratorObj_(\d+)_([^_]+)_(?:\d+)\.json'
        pattern = r'OrchestratorObj_(\d+)_(.+)_\d+\.json'
        match = re.fullmatch(pattern, filename)

        if not match:
            continue

        sequence = int(match.group(1))
        agent_or_init = match.group(2)

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            results.append((sequence, agent_or_init, data))
        except Exception as e:
            print(f"Error loading {filename}: {e}")
            continue

    results.sort(key=lambda x: x[0])

    return results

def extract_run_metadata_from_path(session_dir: str, run_id: int | str) -> dict:
    """
   Extract metadata from session directory path.
   """
    path = Path(session_dir).resolve()
    path_str = str(path)

    timestamp_pattern = r'(\d{4})_(\d{2})_(\d{2})_(\d{2})-(\d{2})-(\d{2})'
    timestamp_match = re.search(timestamp_pattern, path_str)

    run_timestamp = None
    if timestamp_match:
        try:
            year, month, day, hour, minute, second = timestamp_match.groups()
            run_timestamp = f"{year}-{month}-{day}T{hour}:{minute}:{second}+00:00"
        except Exception:
            run_timestamp = None

    uc_pattern = r'(Science_UC|Engineering_UC|Math_UC)'
    uc_match = re.search(uc_pattern, path_str)
    uc = uc_match.group(1) if uc_match else "unknown_UC"

    scenario_pattern = r'scenario_(\w+)'
    scenario_match = re.search(scenario_pattern, path_str)
    scenario = scenario_match.group(1) if scenario_match else f"Scenario{run_id}"

    run_nr_pattern = r'log_(\d+)'
    run_nr_match = re.search(run_nr_pattern, path_str)
    run_nr = int(run_nr_match.group(1)) if run_nr_match else int(str(run_id))

    return {
        "run_timestamp": run_timestamp,
        "uc": uc,
        "scenario": scenario,
        "run_nr": run_nr,
    }
