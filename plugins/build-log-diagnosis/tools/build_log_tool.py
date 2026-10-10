# SPDX-License-Identifier: Apache-2.0
"""Bounded, local evidence extraction. Heuristics are not verified root causes."""
from __future__ import annotations

import re
from pathlib import Path
from openjiuwen.core.foundation.tool import Tool, ToolCard

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SIGNAL = re.compile(r"traceback|\b(?:error|fatal|exception|failed|failure|panic)\b|\w+(?:Error|Exception):", re.I)
RULES = (
    ("missing_dependency", r"ModuleNotFoundError|No module named|Cannot find module", "检查当前解释器/环境与声明的依赖是否一致"),
    ("dependency_conflict", r"ResolutionImpossible|ERESOLVE|conflicting dependencies", "核对日志列出的依赖版本约束"),
    ("test_failure", r"AssertionError|\bFAILED\b.*::", "运行日志中指定的失败用例并核对断言"),
    ("compilation", r"SyntaxError|error TS\d+|fatal error:|error:.*(?:expected|undefined)", "检查编译器报告的文件与行号"),
    ("timeout", r"TimeoutError|timed? out|ETIMEDOUT", "检查对应步骤的超时设置及外部依赖可达性"),
    ("permissions", r"PermissionError|Permission denied|EACCES", "检查执行账号与目标文件权限"),
)


def redact(text):
    text = re.sub(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)((?:password|token|api[_-]?key|secret)\s*[=:]\s*)[^\s,;]+", r"\1[REDACTED]", text)
    return re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)


def diagnose(inputs):
    if ("text" in inputs) == ("log_path" in inputs):
        raise ValueError("Provide exactly one of text or log_path")
    if "log_path" in inputs:
        with Path(inputs["log_path"]).expanduser().open("rb") as handle:
            raw = handle.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError("Log exceeds 2 MB; supply the failing job log")
        text = raw.decode("utf-8", errors="replace")
    else:
        text = inputs["text"]
    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 2_000_000:
        raise ValueError("Expected a non-empty log of at most 2 MB")
    lines = redact(ANSI.sub("", text)).splitlines()
    hits = [i for i, line in enumerate(lines) if SIGNAL.search(line)]
    spans = []
    for i in hits[:100]:
        lo, hi = max(0, i - 3), min(len(lines), i + 6)
        if spans and lo <= spans[-1][1]:
            spans[-1][1] = max(hi, spans[-1][1])
        else:
            spans.append([lo, hi])
    excerpts = [{"start_line": lo + 1, "end_line": hi,
                 "text": "\n".join(lines[lo:hi])} for lo, hi in spans]
    hypotheses = []
    for kind, pattern, next_step in RULES:
        evidence = [i + 1 for i, line in enumerate(lines) if re.search(pattern, line, re.I)]
        if evidence:
            hypotheses.append({"category": kind, "evidence_lines": evidence[:20],
                               "verification": next_step, "verified": False})
    locations = []
    for i in hits[:100]:
        for j in range(max(0, i - 5), min(len(lines), i + 5)):
            match = re.search(r'File "([^"]+)", line (\d+)', lines[j])
            if not match:
                match = re.search(r"([^\s:]+\.(?:py|js|ts|tsx|java|go|rs|c|cpp)):(\d+)", lines[j])
            if match:
                item = {"path": match[1], "line": int(match[2])}
                if item not in locations:
                    locations.append(item)
    return {"success": True, "line_count": len(lines), "signal_count": len(hits),
            "excerpts": excerpts, "hypotheses": hypotheses, "locations": locations,
            "truncated": len(hits) > 100, "root_cause_verified": False,
            "redaction": "best_effort", "build_status": "unknown",
            "note": "No matching signal does not establish build success. Verify hypotheses against source and an actual rerun."}


class BuildLogTool(Tool):
    def __init__(self) -> None:
        super().__init__(ToolCard(id="build_log_diagnosis", name="build_log_diagnosis",
            description="本地提取构建日志中的错误片段、行号与待验证原因，不联网也不自动执行日志命令。",
            input_params={"type": "object", "properties": {
                "text": {"type": "string"}, "log_path": {"type": "string"}},
                "oneOf": [{"required": ["text"]}, {"required": ["log_path"]}]}))

    async def invoke(self, inputs, **kwargs):
        try:
            if not isinstance(inputs, dict):
                raise ValueError("Expected an object")
            return diagnose(inputs)
        except (ValueError, TypeError, OSError) as exc:
            return {"success": False, "error": str(exc)}

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)
