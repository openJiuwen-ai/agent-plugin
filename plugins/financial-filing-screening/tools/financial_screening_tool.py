"""Run the packaged workflow in a separately installed, pinned Python environment."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.tool import Tool, ToolCard


class FinancialScreeningTool(Tool):
    def __init__(self) -> None:
        super().__init__(ToolCard(
            id="financial_filing_screening", name="financial_filing_screening",
            # WorkSwarm's default outer deadline is 300s. Let the bounded worker
            # finish (at most 900s plus 10s cleanup) before the host cancels it.
            properties={"resilience": {"timeout_s": 930}},
            description=("Inspect or screen a local SEC rendered HTML filing folder. "
                         "Screen mode runs the fixed openJiuwen workflow and returns a "
                         "policy decision with a source-cited JSON report. Use inspect first."),
            input_params={
                "type": "object", "additionalProperties": False,
                "properties": {
                    "mode": {"type": "string", "enum": ["inspect", "screen"],
                             "description": "inspect uses no model; screen invokes the configured local model."},
                    "filing_dir": {"type": "string", "description": "Absolute folder containing FilingSummary.xml and R*.htm."},
                    "output_dir": {"type": "string", "description": "Absolute NEW output directory outside the plugin and input folder."},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 900, "default": 900},
                }, "required": ["mode", "filing_dir", "output_dir"],
            }))

    async def invoke(self, inputs: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return await self._invoke(inputs)
        except (OSError, ValueError, TypeError) as exc:
            return {"success": False, "decision": None, "error": str(exc)}

    async def _invoke(self, inputs: Any) -> dict[str, Any]:
        if not isinstance(inputs, dict):
            raise ValueError("Supply mode, filing_dir and output_dir as an object")
        if set(inputs) - {"mode", "filing_dir", "output_dir", "timeout_seconds"}:
            raise ValueError("Unknown input field")
        mode = inputs.get("mode")
        if mode not in ("inspect", "screen"):
            raise ValueError("mode must be inspect or screen")
        timeout = inputs.get("timeout_seconds", 900)
        if type(timeout) is not int or not 1 <= timeout <= 900:
            raise ValueError("timeout_seconds must be an integer between 1 and 900")
        filing = self._path(inputs.get("filing_dir"), "filing_dir")
        output = self._path(inputs.get("output_dir"), "output_dir")
        package = Path(__file__).resolve().parents[1]
        if not filing.is_dir() or not (filing / "FilingSummary.xml").is_file():
            raise ValueError("filing_dir must contain FilingSummary.xml")
        if output.is_relative_to(package) or output.is_relative_to(filing):
            raise ValueError("output_dir must be outside the plugin and input filing")
        if output.exists() or not output.parent.is_dir():
            raise ValueError("output_dir must be new and its parent directory must exist")
        interpreter = self._path(os.environ.get("FINANCIAL_SCREENING_PYTHON"), "FINANCIAL_SCREENING_PYTHON")
        if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
            raise ValueError("FINANCIAL_SCREENING_PYTHON must name an executable Python interpreter")
        if mode == "screen" and (os.environ.get("FINANCIAL_SCREENING_ALLOW_INFERENCE") != "1"
                                  or not os.environ.get("FINANCIAL_SCREENING_MODEL", "").strip()):
            raise ValueError("Configure FINANCIAL_SCREENING_MODEL and FINANCIAL_SCREENING_ALLOW_INFERENCE=1 first")
        # Environment configuration is operator-controlled, never a model-supplied command.
        env = {key: value for key, value in os.environ.items()
               if key in {"PATH", "HOME", "USER", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "WINDIR",
                          "FINANCIAL_SCREENING_MODEL", "FINANCIAL_SCREENING_ALLOW_INFERENCE"}}
        env.update({"PYTHONDONTWRITEBYTECODE": "1", "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"})
        output.mkdir(mode=0o700)
        command = [str(interpreter), "-I", "-B", str(package / "runtime/run_filing.py"),
                   "--mode", mode, "--filing-dir", str(filing), "--output-dir", str(output),
                   "--timeout-seconds", str(timeout)]
        with (output / "runtime.log").open("wb") as log:
            process = await asyncio.create_subprocess_exec(
                *command, cwd=str(output), env=env,
                stdout=log, stderr=asyncio.subprocess.STDOUT)
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout + 10)
            except (TimeoutError, asyncio.CancelledError) as exc:
                if process.returncode is None:
                    process.kill()
                await process.wait()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return {"success": False, "decision": None, "error": "Workflow timed out",
                        "output_dir": str(output)}
        result_path = output / "result.json"
        if not result_path.is_file() or result_path.stat().st_size > 2_000_000:
            return {"success": False, "decision": None,
                    "error": "Worker produced no usable result. Check runtime.log and installed dependencies.",
                    "output_dir": str(output)}
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("success") is not True or process.returncode != 0:
            # Keep raw diagnostic text local: it may include filing excerpts.
            return {"success": False, "decision": None,
                    "error": "Workflow did not complete. Review result.json and runtime.log locally.",
                    "output_dir": str(output)}
        if mode == "screen":
            report_path = output / "report.json"
            if payload.get("decision") not in {"PASS", "REVIEW", "REJECT"} or not report_path.is_file():
                return {"success": False, "decision": None, "error": "Missing decision or report artifact"}
        payload["output_dir"] = str(output)
        return payload

    @staticmethod
    def _path(value: Any, label: str) -> Path:
        if not isinstance(value, str) or not value.strip() or not Path(value).is_absolute():
            raise ValueError(f"{label} must be an absolute path")
        # Do not resolve interpreter symlinks: doing so bypasses virtual environments.
        return Path(value) if label == "FINANCIAL_SCREENING_PYTHON" else Path(value).resolve()

    async def stream(self, inputs: Any, **kwargs: Any):
        yield await self.invoke(inputs, **kwargs)
