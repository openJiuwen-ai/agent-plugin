"""Isolated entry point for the SEC HTML workflow used in the WorkSwarm demo."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import sys
import time


def validate_filing(directory: Path) -> None:
    """Reject incomplete bundles and references outside the supplied filing folder."""
    if not directory.is_dir():
        raise ValueError("filing_dir must be an existing directory")
    summary = directory / "FilingSummary.xml"
    if summary.is_symlink() or not summary.is_file():
        raise ValueError("A regular FilingSummary.xml is required")
    if summary.stat().st_size > 5_000_000:
        raise ValueError("FilingSummary.xml exceeds 5 MB")
    from screening.ingestion.html import report_titles
    reports = report_titles(summary.read_text(encoding="utf-8", errors="replace"))
    if not reports or len(reports) > 1000:
        raise ValueError("The filing must list 1 to 1000 rendered reports")
    total = 0
    for _, filename in reports:
        if not re.fullmatch(r"R[0-9]+\.html?", filename, flags=re.IGNORECASE):
            raise ValueError("Report references must be local R<number>.htm or .html files")
        path = directory / filename
        if path.is_symlink() or not path.is_file():
            raise ValueError("Every listed report must be present as a regular local file")
        total += path.stat().st_size
    if total > 200_000_000:
        raise ValueError("The rendered filing exceeds 200 MB")


async def run(args) -> dict:
    from screening.ingestion.html import parse_html_filing, infer_html_filing_identity
    validate_filing(args.filing_dir)
    if args.mode == "inspect":
        document = parse_html_filing(args.filing_dir)
        company, year = infer_html_filing_identity(document)
        return {
            "success": True, "status": "INSPECTED", "mode": "inspect",
            "company_name": company, "fiscal_year": year,
            "reports": [{"report_id": p.printed_document_page,
                         "title": p.raw_text.splitlines()[0],
                         "line_count": len(p.raw_text.splitlines())} for p in document.pages],
            "decision": None, "model_calls": 0,
        }

    model = os.environ.get("FINANCIAL_SCREENING_MODEL", "").strip()
    if not model or os.environ.get("FINANCIAL_SCREENING_ALLOW_INFERENCE") != "1":
        raise ValueError("Set FINANCIAL_SCREENING_MODEL and FINANCIAL_SCREENING_ALLOW_INFERENCE=1")
    from screening.domain import PolicyThresholds
    from screening.extraction.openjiuwen import load_extraction_config
    from screening.workflows.screening import (
        DeterministicFinancialCalculationsComponent, ScreeningWorkflowInput,
        run_screening_workflow,
    )
    # Never inherit the host agent's provider, keys or endpoint. This first release
    # supports the same loopback OpenAI-compatible vLLM endpoint as the demo.
    config = load_extraction_config({
        "OPENJIUWEN_MODEL_PROVIDER": "ollama",
        "OPENJIUWEN_MODEL_NAME": model,
        "OPENJIUWEN_API_BASE": "http://127.0.0.1:11434/v1",
        "OPENJIUWEN_EXTRACTION_MAX_TOKENS": "1024",
    })
    policy = PolicyThresholds()
    os.environ["WORKFLOW_EXECUTE_TIMEOUT"] = str(args.timeout_seconds)
    # Observe the same component as the demo to preserve extracted financials.
    financials = None
    original = DeterministicFinancialCalculationsComponent.invoke

    async def capture(component, inputs, session, context):
        nonlocal financials
        output = await original(component, inputs, session, context)
        financials = output["financials"]
        return output

    DeterministicFinancialCalculationsComponent.invoke = capture
    try:
        result = await asyncio.wait_for(run_screening_workflow(
            ScreeningWorkflowInput(filing_path=args.filing_dir, policy=policy), config,
            financial_top_k=6, source_format="html"), timeout=args.timeout_seconds)
    finally:
        DeterministicFinancialCalculationsComponent.invoke = original
    if financials is None:
        raise ValueError("Workflow completed without captured financial evidence")
    report = result.model_dump(mode="json")
    report["financials"] = financials.model_dump(mode="json")
    (args.output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "success": True, "status": "COMPLETED", "mode": "screen",
        "company_name": result.company_name, "fiscal_year": result.fiscal_year,
        "decision": result.decision.value, "reasons": list(result.decision_reasons),
        "report_path": str(args.output_dir / "report.json"),
        "policy": policy.model_dump(mode="json"),
        "usage": "Not measured. No benchmark or speed claim is made by this run.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("inspect", "screen"))
    parser.add_argument("--filing-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args()
    if not args.filing_dir.is_absolute() or not args.output_dir.is_absolute():
        parser.error("filing-dir and output-dir must be absolute paths")
    if not 1 <= args.timeout_seconds <= 900:
        parser.error("timeout-seconds must be between 1 and 900")
    runtime = Path(__file__).resolve().parent
    args.filing_dir = args.filing_dir.resolve()
    args.output_dir = args.output_dir.resolve()
    if args.output_dir.is_relative_to(runtime.parent) or args.output_dir.is_relative_to(args.filing_dir):
        parser.error("output-dir must be outside the plugin and filing directories")
    # The tool creates this private directory; direct CLI users must create it too.
    if not args.output_dir.is_dir() or any(args.output_dir.glob("*.json")):
        parser.error("output-dir must exist and contain no JSON results")
    sys.path.insert(0, str(runtime))
    started = time.monotonic()
    try:
        payload = asyncio.run(run(args))
    except Exception as exc:
        payload = {"success": False, "status": "EXECUTION_ERROR", "mode": args.mode,
                   "decision": None, "error_type": type(exc).__name__, "error": str(exc)}
    payload["elapsed_seconds"] = round(time.monotonic() - started, 3)
    (args.output_dir / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if payload["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
