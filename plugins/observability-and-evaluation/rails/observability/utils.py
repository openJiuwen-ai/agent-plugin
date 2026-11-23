"""General workflow utilities for prompts, dataset selection,
file loading, answer extraction, tool-result envelopes, and scenario metadata lookup."""
import os
import json
import string
import re
from pathlib import Path
from typing import Dict, Any
from .emit.common import safe_text
# Phase 1 step 8: ONE copy each. The envelope writer/reader pair is
# re-exported here because the use-case tooling imports it from utils -
# the stable facade - while the single definitions live in emit/tools.py
# (get_final_agent_output's third copy here was dead and is simply gone;
# callers import it from emit.extract).
from .emit.tools import make_tool_result, parse_structured_tool_output

class PromptTemplate:
    """Stores a system prompt, user prompt, and placeholder names.

    The class renders prompt dictionaries for agents while checking that all required variables have been supplied.
    """
    def __init__(self, system: str, user: str, variables: list[str] | None = None):
        """Store prompt text and either use explicit variables or infer placeholders from the templates."""
        self.system = system
        self.user = user
        # If the author supplied an explicit list, trust it; otherwise infer.
        self.variables = set(variables or self.extract_placeholders(system + user))

    @staticmethod
    def extract_placeholders(text: str) -> list[str]:
        """Return all format-placeholder names found in a prompt string."""
        return [name for _, name, _, _ in string.Formatter().parse(text) if name]

    def render(self, **kwargs) -> dict[str, str]:
        """Fill the system/user templates with runtime variables and reject missing placeholders early."""
        missing = self.variables - kwargs.keys()
        if missing:
            raise ValueError(f"Missing values for placeholders: {missing}")

        return {
            "system": self.system.format(**kwargs),
            "user":   self.user.format(**kwargs),
        }

def ask_topic(default: str = "Agentic Memory", preset: str | None = None) -> str:
    """Prompt the user for a research topic, returning a default when input is empty or unavailable.

    `preset` is the non-interactive path (--topic): when given, it is returned
    without prompting, falling back to `default` if it strips to nothing.
    """
    if preset is not None:
        return preset.strip() or default
    try:
        raw = input(f"Enter research topic [Press Enter for default topic: {default}] ").strip()
    except EOFError:
        return default
    return raw or default

def maybe_print_traces(otel_traces, observability):
    """Print captured OTel traces only when traces exist and observability is configured to show them."""
    if otel_traces and observability.print_traces:
        print(f"\n\n{'='*60}\n OTel Traces: \n{'='*60}\n")
        print(otel_traces)



def select_dataset(
    preset: str | None = None,
    gsm8k_path: str | os.PathLike | None = None,
    gsmplus_path: str | os.PathLike | None = None,
) -> tuple[str, list[dict]]:
    """Ask which dataset to use, load it, and return (name, records).

    Each record is normalized to {"question": str, "groundtruth": str}
    so callers don't need to know GSM8K vs GSM-Plus schema differences.

    `preset` is the non-interactive path (--dataset gsm8k|gsm-plus, already
    canonicalized by ojw_agentic_applications.config.datasets, which is where
    the CLI aliases live): when given, no prompt is shown. The dataset file
    locations come from the
    explicit path arguments (the math scenarios pass them from
    config/paths.py, resolved against the repository root); the env vars and
    the CWD-relative literals remain only as fallbacks for direct callers.
    This module must not import ojw_agentic_applications, which is why the
    paths arrive as parameters.
    """
    gsm8k_path = str(gsm8k_path) if gsm8k_path else os.environ.get(
        "GSM8K_Dataset_PATH",
        r"./Dataset/gsm8k/main/train-00000-of-00001.parquet",
    )
    gsmplus_path = str(gsmplus_path) if gsmplus_path else os.environ.get(
        "GSMplus_Dataset_PATH",
        r"./Dataset/GSM-Plus/data/test-00000-of-00001.jsonl",
    )

    preset_to_choice = {"gsm8k": "1", "1": "1", "gsm-plus": "2", "gsmplus": "2", "2": "2"}
    if preset is not None:
        forced_choice = preset_to_choice.get(str(preset).strip().lower())
        if forced_choice is None:
            raise ValueError(f"Unknown dataset {preset!r}; expected 'gsm8k' or 'gsm-plus'.")
    else:
        forced_choice = None
        print(f"\n{'=' * 60}\n Select Dataset \n{'=' * 60}")
        print("  1. GSM8K (0-7472)")
        print("  2. GSM-Plus (0 - 10551)")

    while True:
        if forced_choice is not None:
            choice = forced_choice
        else:
            choice = input("Enter dataset number (1 or 2): ").strip()
            print(choice)
        if choice == "1":
            import pandas as pd
            df = pd.read_parquet(gsm8k_path)
            records = [
                {"question": row["question"], "groundtruth": row["answer"]}
                for _, row in df.iterrows()
            ]
            print(f"Loaded GSM8K: {len(records)-1} records.")
            return "GSM8K", records

        if choice == "2":
            decoder = json.JSONDecoder()
            with open(gsmplus_path, "r", encoding="utf-8") as f:
                content = f.read()

            records = []
            skipped = 0
            idx = 0
            n = len(content)
            while idx < n:
                while idx < n and content[idx] in " \t\r\n":
                    idx += 1
                if idx >= n:
                    break
                try:
                    obj, end = decoder.raw_decode(content, idx)
                except json.JSONDecodeError:
                    next_start = content.find('{"question"', idx + 1)
                    if next_start == -1:
                        break
                    skipped += 1
                    idx = next_start
                    continue

                records.append(
                    {"question": obj["question"], "groundtruth": obj["solution"]}
                )
                idx = end

            print(f"Loaded GSM-Plus: {len(records)} records ({skipped} malformed records skipped).")
            return "GSM-Plus", records

        print("Invalid choice — enter 1 or 2.")



def build_prompt_library(raw_json) -> Dict[str, PromptTemplate]:
    """Convert raw prompt JSON into PromptTemplate objects keyed by agent name."""
    library = {}
    for name, spec in raw_json.items():
        library[name] = PromptTemplate(
            system=spec["system"],
            user=spec["user"],
            variables=spec.get("variables"),
        )
    return library


def load_file_content(filepath):
    """Read a text/template file if it exists, otherwise warn and return None."""
    try:
        if not os.path.exists(filepath):
            print(f"   [System] Warning: Template file '{filepath}' not found. Using default structure.")
            return None
        with open(filepath, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"   [System] Error reading template: {e}")
        return None


def load_pdf_content(filepath):
    """Reads text content from a PDF file safely."""
    try:
        from pypdf import PdfReader
        if not os.path.exists(filepath):
            print(f"   [System] Warning: File '{filepath}' not found.")
            return None

        # Open the PDF and extract text from all pages
        reader = PdfReader(filepath)
        text = ""
        for page in reader.pages:
            text += page.extract_text() + "\n"

        return text

    except Exception as e:
        print(f"   [System] Error reading PDF: {e}")
        return None




def load_humaneval_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """Load and validate a HumanEval-style JSONL dataset from disk."""
    path = Path(path).expanduser().resolve()

    if not path.exists():
        raise FileNotFoundError(f"HumanEval JSONL file not found: {path}")

    data: list[dict[str, Any]] = []

    with path.open("r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            line = line.strip()

            if not line:
                continue

            try:
                data.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise RuntimeError(
                    f"Invalid JSON on line {line_num} in {path}: {e}"
                ) from e

    if not data:
        raise RuntimeError(f"No valid JSON records loaded from {path}")

    return data

def parse_qa_verdict(qa_text: str) -> bool:
    """
    Returns True if QA passed, False if failed.
    Looks for QA-PASS / QA-FAILED anywhere in the response (case-insensitive).
    Defaults to False (treat as failed) if neither token is found.
    """
    upper = qa_text.upper()
    if "QA-PASS" in upper:
        return True
    if "QA-FAILED" in upper:
        return False
    # No explicit verdict found — treat conservatively as failed
    return False


def Otel_trace_choice(prompt: str, preset: int | bool | None = None) -> bool:
    """Ask a yes/no question where the user must enter 1 for yes or 0 for no.

    `preset` is the non-interactive path (--print-traces 0|1): when given, its
    truthiness is returned without prompting.
    """
    if preset is not None:
        return bool(preset)
    while True:
        raw = input(prompt).strip()
        print(raw)
        if raw == "1":
            return True
        if raw == "0":
            return False
        print("Please enter 1 for yes or 0 for no.")


def ask_non_negative_int(prompt: str, preset: int | None = None) -> int:
    """Prompt until the user enters zero or a positive whole number.

    `preset` is the non-interactive path: when given, it is validated and
    returned without prompting. The runner reaches this through --task-id, via
    ojw_agentic_applications.config.datasets.resolve_task, which also accepts a
    dataset's native task id - this function only understands row numbers.
    """
    if preset is not None:
        n = int(preset)
        if n < 0:
            raise ValueError(f"Expected a non-negative integer, got {preset!r}")
        return n
    while True:
        raw = input(prompt).strip()
        print(raw)
        try:
            n = int(raw)
            if n < 0:
                print("Please enter zero or a positive integer: ")
                continue
            return n
        except ValueError:
            print("Invalid input. Please enter a whole number: ")

def extract_python_code(response) -> str:
    """
    Extract Python code from an agent response.

    Supports:
    - raw string responses
    - AgentResponse objects with .text
    - markdown ```python ... ``` blocks
    - fallback to full text if no code block exists
    """

    # Convert AgentResponse to text if needed
    if hasattr(response, "text"):
        response = response.text

    # Convert anything else safely to string
    if not isinstance(response, str):
        response = str(response)

    # Match ```python ... ```
    pattern = r"```(?:python|py)?\s*(.*?)```"
    matches = re.findall(pattern, response, re.DOTALL | re.IGNORECASE)

    if matches:
        return "\n\n".join(match.strip() for match in matches)

    # Fallback: return full response if no markdown code block found
    return response.strip()

def get_scenario_metadata(registry_path: str, domain: str, scenario: int | str) -> dict:
    """
    Look up one scenario's metadata from all_scenario_Information.json.

    Returns:
      {
        "uc": "science",
        "scenario": "scenario_1",
        "framework": "OJW",
        "system_architecture": "sequential",   # normalized from "type"
        "system_architecture_description": "...long text...",
      }
    Raises KeyError if the domain/scenario is not in the registry.
    """
    domain_key = str(domain).strip().lower()
    scenario_key = f"scenario{scenario}"

    with Path(registry_path).expanduser().resolve().open("r", encoding="utf-8") as f:
        registry = json.load(f)

    if domain_key not in registry:
        raise KeyError(f"domain '{domain_key}' not found. Available: {list(registry)}")
    if scenario_key not in registry[domain_key]:
        raise KeyError(
            f"'{scenario_key}' not found under '{domain_key}'. "
            f"Available: {list(registry[domain_key])}"
        )

    entry = registry[domain_key][scenario_key]

    # Preserve shared-context execution as a distinct architecture label.
    # These workflows use the flat agent-trace container, like sequential
    # workflows, but all agents share conversation history. Unknown registry
    # types still fail before execution or export.
    type_to_arch = {
        "sequential":     "sequential",
        "shared_context": "shared_context",
        "orchestrator":   "hierarchical",
        "looping":        "looping",
    }
    raw_type = str(entry.get("type", "")).strip().lower()
    if raw_type not in type_to_arch:
        raise ValueError(
            f"'{domain_key}/{scenario_key}' has type {entry.get('type')!r}, which maps to no "
            f"system_architecture. Known types: {sorted(type_to_arch)}."
        )

    return {
        "uc": entry.get("uc", domain_key),
        "scenario": entry.get("scenario", scenario_key),
        "framework": entry.get("framework", ""),
        "system_architecture": type_to_arch[raw_type],
        "system_architecture_description": entry.get("system_architecture", ""),
    }
