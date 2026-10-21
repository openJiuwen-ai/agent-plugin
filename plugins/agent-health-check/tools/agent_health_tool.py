"""第三方 Agent / 智能体 CLI 健康检测工具。

扫描本机已知的 AI 编程/对话 Agent 命令行工具（codex、Claude Code、Kimi Code、
Zcode 等），检测安装状态、版本、配置与登录凭据、可选网络连通性，输出结构化
健康报告。全程只读：不读取密钥内容、不修改任何文件。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from openjiuwen.core.foundation.tool import Tool, ToolCard

KNOWN_AGENTS: list[dict] = [
    {
        "id": "codex",
        "name": "Codex CLI (OpenAI)",
        "commands": ["codex"],
        "version_args": ["--version"],
        "config_dirs": [".codex"],
        "config_files": [".codex/auth.json", ".codex/config.toml"],
        "env_keys": ["OPENAI_API_KEY", "CODEX_API_KEY"],
        "endpoint_host": "api.openai.com",
        "install_hint": "npm install -g @openai/codex  或  参考 https://github.com/openai/codex",
    },
    {
        "id": "claudecode",
        "name": "Claude Code (Anthropic)",
        "commands": ["claude"],
        "version_args": ["--version"],
        "config_dirs": [".claude"],
        "config_files": [".claude.json", ".claude/settings.json"],
        "env_keys": ["ANTHROPIC_API_KEY", "CLAUDE_CODE_API_KEY"],
        "endpoint_host": "api.anthropic.com",
        "install_hint": "npm install -g @anthropic-ai/claude-code",
    },
    {
        "id": "kimicode",
        "name": "Kimi Code (Moonshot)",
        "commands": ["kimi", "kimi-cli"],
        "version_args": ["--version"],
        "config_dirs": [".kimi"],
        "config_files": [".kimi/config.json", ".kimi/config.toml"],
        "env_keys": ["MOONSHOT_API_KEY", "KIMI_API_KEY"],
        "endpoint_host": "api.moonshot.cn",
        "install_hint": "参考 Moonshot 官方文档安装 Kimi CLI",
    },
    {
        "id": "zcode",
        "name": "Z Code",
        "commands": ["zcode", "z-code"],
        "version_args": ["--version"],
        "config_dirs": [".zcode"],
        "config_files": [".zcode/config.json"],
        "env_keys": ["ZCODE_API_KEY", "Z_API_KEY"],
        "endpoint_host": None,
        "install_hint": "参考 Z Code 官方渠道安装",
    },
    {
        "id": "gemini-cli",
        "name": "Gemini CLI (Google)",
        "commands": ["gemini"],
        "version_args": ["--version"],
        "config_dirs": [".gemini"],
        "config_files": [".gemini/settings.json"],
        "env_keys": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        "endpoint_host": "generativelanguage.googleapis.com",
        "install_hint": "npm install -g @google/gemini-cli",
    },
    {
        "id": "cursor-agent",
        "name": "Cursor Agent CLI",
        "commands": ["cursor-agent", "cursor"],
        "version_args": ["--version"],
        "config_dirs": [".cursor"],
        "config_files": [],
        "env_keys": ["CURSOR_API_KEY"],
        "endpoint_host": None,
        "install_hint": "参考 Cursor 官方文档安装 cursor-agent",
    },
    {
        "id": "aider",
        "name": "Aider",
        "commands": ["aider"],
        "version_args": ["--version"],
        "config_dirs": [".aider"],
        "config_files": [".aider.conf.yml"],
        "env_keys": ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"],
        "endpoint_host": None,
        "install_hint": "pip install aider-chat",
    },
    {
        "id": "copilot-cli",
        "name": "GitHub Copilot CLI",
        "commands": ["copilot", "gh-copilot"],
        "version_args": ["--version"],
        "config_dirs": [".copilot"],
        "config_files": [],
        "env_keys": ["GH_TOKEN", "GITHUB_TOKEN"],
        "endpoint_host": "api.github.com",
        "install_hint": "参考 GitHub Copilot CLI 官方文档安装",
    },
]

_STATUS_HEALTHY = "healthy"
_STATUS_DEGRADED = "degraded"
_STATUS_MISSING = "missing"
_STATUS_ERROR = "error"


def _find_executable(commands: list[str]) -> str | None:
    """Return the first resolvable command path, or None."""
    for cmd in commands:
        path = shutil.which(cmd)
        if path:
            return path
    return None


def _run_version(executable: str, version_args: list[str], timeout: float) -> tuple[str | None, str | None]:
    """Run `<executable> --version`, return (version_text, error)."""
    kwargs: dict = {
        "capture_output": True,
        "text": True,
        "timeout": timeout,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run([executable, *version_args], **kwargs)
    except subprocess.TimeoutExpired:
        return None, f"version check timed out after {timeout}s"
    except OSError as exc:
        return None, f"failed to execute: {exc}"
    output = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    first_line = output.splitlines()[0].strip() if output else ""
    if proc.returncode != 0 and not first_line:
        return None, f"exit code {proc.returncode} with no output"
    return first_line or None, None


def _check_config(spec: dict) -> dict:
    """Check config dirs/files existence under user home (read-only, metadata only)."""
    home = Path.home()
    dirs = {d: (home / d).is_dir() for d in spec.get("config_dirs", [])}
    files = {f: (home / f).is_file() for f in spec.get("config_files", [])}
    env = {k: bool(os.environ.get(k)) for k in spec.get("env_keys", [])}
    return {
        "config_dirs": dirs,
        "config_files": files,
        "env_keys": env,
        "any_config_present": any(dirs.values()) or any(files.values()),
        "any_env_present": any(env.values()),
    }


def _probe_tcp(host: str, timeout: float, port: int = 443) -> dict:
    """TCP-connect probe to host:port; returns reachability and latency."""
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            latency_ms = round((time.monotonic() - started) * 1000, 1)
            return {"reachable": True, "latency_ms": latency_ms, "host": host, "port": port}
    except (OSError, socket.timeout) as exc:
        return {
            "reachable": False,
            "latency_ms": None,
            "host": host,
            "port": port,
            "error": str(exc),
        }


def _check_agent(spec: dict, timeout: float, check_network: bool) -> dict:
    """Run the full health check pipeline for one agent spec."""
    result: dict = {
        "id": spec["id"],
        "name": spec["name"],
        "status": _STATUS_MISSING,
        "installed": False,
        "executable": None,
        "version": None,
        "issues": [],
        "hints": [],
    }
    executable = _find_executable(spec["commands"])
    if not executable:
        result["issues"].append("命令不在 PATH 中（尝试过: " + ", ".join(spec["commands"]) + "）")
        result["hints"].append(spec["install_hint"])
        return result

    result["installed"] = True
    result["executable"] = executable
    result["status"] = _STATUS_HEALTHY

    version, err = _run_version(executable, spec["version_args"], timeout)
    if version:
        result["version"] = version
    elif err:
        result["issues"].append(f"版本探测失败: {err}")
        result["status"] = _STATUS_DEGRADED

    config = _check_config(spec)
    result["config"] = config
    if not config["any_config_present"] and not config["any_env_present"]:
        result["issues"].append("未发现配置目录/文件，也未设置鉴权环境变量，可能未登录")
        result["hints"].append("请执行该工具的登录/初始化命令，或设置对应 API Key 环境变量")
        result["status"] = _STATUS_DEGRADED

    host = spec.get("endpoint_host")
    if check_network and host:
        net = _probe_tcp(host, timeout)
        result["network"] = net
        if not net["reachable"]:
            result["issues"].append(f"无法连通 API 端点 {host}:443（{net['error']}）")
            result["hints"].append("检查网络/代理设置，或确认服务是否可用")
            result["status"] = _STATUS_DEGRADED
    return result


def _summarize(items: list[dict]) -> dict:
    counts = {_STATUS_HEALTHY: 0, _STATUS_DEGRADED: 0, _STATUS_MISSING: 0, _STATUS_ERROR: 0}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    installed = sum(1 for it in items if it["installed"])
    overall = _STATUS_HEALTHY
    if counts[_STATUS_DEGRADED] or counts[_STATUS_ERROR]:
        overall = _STATUS_DEGRADED
    if installed == 0:
        overall = _STATUS_MISSING
    return {
        "overall": overall,
        "total": len(items),
        "installed": installed,
        "healthy": counts[_STATUS_HEALTHY],
        "degraded": counts[_STATUS_DEGRADED],
        "missing": counts[_STATUS_MISSING],
    }


class AgentHealthCheckTool(Tool):
    """检测本机第三方 Agent/智能体 CLI 的健康状态。"""

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id="agent_health_check",
                name="agent_health_check",
                description=(
                    "检测本机第三方 AI Agent / 智能体 CLI 工具（codex、Claude Code、Kimi Code、"
                    "Zcode、Gemini CLI、Cursor Agent、Aider、Copilot CLI 等）的健康状态：是否安装、"
                    "版本、配置/登录状态、可选的 API 端点连通性。用户要求体检、排查 Agent 不可用、"
                    "检查安装与登录状态时调用。全程只读，不读取密钥内容。"
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "targets": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "要检测的目标 id 列表（如 [\"codex\", \"claudecode\"]）；"
                                "留空检测全部已知 Agent。可用 id：" + ", ".join(a["id"] for a in KNOWN_AGENTS)
                            ),
                        },
                        "check_network": {
                            "type": "boolean",
                            "description": "是否对已知 API 端点做 TCP:443 连通性探测，默认 false",
                            "default": False,
                        },
                        "timeout_seconds": {
                            "type": "number",
                            "description": "单次版本探测/网络探测的超时秒数，默认 8，范围 1-30",
                            "default": 8,
                        },
                    },
                    "required": [],
                },
            )
        )

    async def invoke(self, inputs, **kwargs):
        try:
            if isinstance(inputs, str):
                inputs = json.loads(inputs) if inputs.strip() else {}
            if not isinstance(inputs, dict):
                return {"success": False, "error": "inputs 必须是 JSON object"}
            return self._run(inputs)
        except Exception as exc:  # noqa: BLE001 - tool must fail open
            return {"success": False, "error": f"health check failed: {exc}"}

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)

    def _run(self, inputs: dict) -> dict:
        targets = inputs.get("targets") or []
        check_network = bool(inputs.get("check_network", False))
        try:
            timeout = float(inputs.get("timeout_seconds", 8))
        except (TypeError, ValueError):
            timeout = 8.0
        timeout = min(max(timeout, 1.0), 30.0)

        specs = KNOWN_AGENTS
        unknown: list[str] = []
        if targets:
            wanted = {str(t).strip().lower() for t in targets}
            specs = [a for a in KNOWN_AGENTS if a["id"] in wanted or a["name"].lower() in wanted]
            matched = {a["id"] for a in specs} | {a["name"].lower() for a in specs}
            unknown = sorted(t for t in wanted if t not in matched)

        items = [_check_agent(spec, timeout, check_network) for spec in specs]
        summary = _summarize(items)
        return {
            "success": True,
            "summary": summary,
            "items": items,
            "unknown_targets": unknown,
            "options": {"check_network": check_network, "timeout_seconds": timeout},
        }
