# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Docker 容器诊断工具：只读 Docker CLI 白名单封装。

继承 ``openjiuwen.core.foundation.tool.Tool``。仅执行只读子命令
（ps / inspect / logs）：无 shell、列表传参、容器名/ID 严格校验
（不可为选项、不含空白）、超时与输出长度有界。inspect 结果只保留
精选字段，Env / Labels / 命令行 / 健康日志等敏感字段一律不返回；
日志按常见凭据模式尽力脱敏并明确标注 best-effort。
绝不执行 start/stop/restart/exec/build/pull/compose 等变更操作。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any

from openjiuwen.core.foundation.tool import Tool, ToolCard

_CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_REDACTION_RULES = (
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
            re.S,
        ),
        "<REDACTED:PRIVATE_KEY>",
    ),
    (
        re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}"),
        "Bearer <REDACTED>",
    ),
    (
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|"
            r"access[_-]?key|authorization)\b\s*[=:]\s*"
            r"(\"[^\"]*\"|'[^']*'|(?!bearer\b)[^\s,;]+)"
        ),
        r"\1=<REDACTED>",
    ),
    (
        re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
        "AKIA<REDACTED>",
    ),
)


def _run_docker(args: list[str], timeout: float):
    """执行 docker CLI（无 shell、列表传参）。

    返回 (returncode, stdout, stderr) 字符串元组；CLI 缺失返回 None；
    超时抛出 subprocess.TimeoutExpired。模块级函数便于测试注入替身。
    """
    exe = shutil.which("docker")
    if exe is None:
        return None
    proc = subprocess.run(
        [exe] + list(args),
        capture_output=True,
        timeout=timeout,
    )
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", errors="replace"),
        proc.stderr.decode("utf-8", errors="replace"),
    )


class DockerInspectorTool(Tool):
    """只读诊断本机 Docker：list_containers / inspect_container / logs。"""

    DEFAULT_TIMEOUT_SECONDS = 10.0
    MAX_TIMEOUT_SECONDS = 30.0
    DEFAULT_LIMIT = 50
    MAX_LIMIT = 200
    DEFAULT_TAIL = 100
    MAX_TAIL = 500
    MAX_OUTPUT_CHARS = 20000
    MAX_LINE_CHARS = 2000
    MAX_ERROR_DETAIL_CHARS = 500
    _DAEMON_DOWN_MARKERS = (
        "cannot connect to the docker daemon",
        "error during connect",
        "the docker daemon is not running",
        "failed to connect to the docker api",
        "check if the path is correct and if the daemon is running",
    )

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id="docker_inspector",
                name="docker_inspector",
                description=(
                    "Docker 容器只读诊断工具：list_containers 列出本机容器（含已停止）；"
                    "inspect_container 查看指定容器的状态 / 健康 / 重启次数 / 端口映射"
                    "（Env、Labels、命令行、健康日志等敏感字段不返回）；"
                    "logs 查看最近日志并按常见凭据模式尽力脱敏。"
                    "CLI 缺失或守护进程不可达返回明确 success=false。"
                    "仅执行只读命令（ps/inspect/logs），无 shell、容器名严格校验、"
                    "超时与输出有界；绝不 start/stop/restart/exec/build/pull。"
                    "需要排查容器运行状态或查看日志时调用。"
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["list_containers", "inspect_container", "logs"],
                            "description": (
                                "操作：list_containers=容器清单；"
                                "inspect_container=单容器状态详情；logs=最近日志（脱敏）"
                            ),
                        },
                        "container": {
                            "type": "string",
                            "description": (
                                "inspect_container / logs 必填：容器名或 ID"
                                "（字母数字开头，不可为选项、不含空白）"
                            ),
                        },
                        "tail": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 500,
                            "description": "logs 返回的末尾行数（默认 100）",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 200,
                            "description": "list_containers 最多返回条数（默认 50）",
                        },
                        "timeout": {
                            "type": "number",
                            "minimum": 1,
                            "maximum": 30,
                            "description": "单次 CLI 调用超时秒数（默认 10）",
                        },
                    },
                    "required": ["action"],
                },
            )
        )

    async def invoke(self, inputs: Any, **kwargs: Any) -> dict[str, Any]:
        params = self._extract_params(inputs)
        action = params.get("action")
        if action not in ("list_containers", "inspect_container", "logs"):
            return {
                "success": False,
                "error": (
                    "参数 action 必须为 'list_containers' / 'inspect_container' / 'logs'"
                ),
            }
        timeout = self._bounded_timeout(params.get("timeout"))
        if action == "list_containers":
            return self._list_containers(params, timeout)
        container = params.get("container")
        if not isinstance(container, str) or not _CONTAINER_RE.match(container.strip()):
            return {
                "success": False,
                "error": (
                    "缺少或非法参数 container：仅接受字母数字开头、"
                    "仅含 [A-Za-z0-9_.-] 的容器名或 ID（不能是选项、不含空白）"
                ),
            }
        container = container.strip()
        if action == "inspect_container":
            return self._inspect_container(container, timeout)
        return self._logs(container, params, timeout)

    async def stream(self, inputs: Any, **kwargs: Any):
        yield await self.invoke(inputs, **kwargs)

    @staticmethod
    def _extract_params(inputs: Any) -> dict[str, Any]:
        keys = ("action", "container", "tail", "limit", "timeout")
        if isinstance(inputs, dict):
            return {k: inputs[k] for k in keys if k in inputs}
        result: dict[str, Any] = {}
        for k in keys:
            v = getattr(inputs, k, None)
            if v is not None:
                result[k] = v
        return result

    def _bounded_timeout(self, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return self.DEFAULT_TIMEOUT_SECONDS
        if value < 1:
            return 1.0
        return min(float(value), self.MAX_TIMEOUT_SECONDS)

    def _exec_readonly(self, args: list[str], timeout: float):
        """执行只读 docker 命令；失败时返回 (None, error_dict)。"""
        try:
            result = _run_docker(args, timeout)
        except subprocess.TimeoutExpired:
            return None, {
                "success": False,
                "error": (
                    f"docker 命令超时（{timeout:.0f} 秒）已中止："
                    "可用 timeout 参数调整（上限 30 秒）"
                ),
            }
        except OSError as exc:
            return None, {"success": False, "error": f"无法执行 docker CLI：{exc}"}
        if result is None:
            return None, {
                "success": False,
                "error": (
                    "未找到 docker CLI：请确认 Docker 已安装并在 PATH 中"
                    "（本工具不会安装或启动 Docker）"
                ),
            }
        rc, stdout, stderr = result
        if rc != 0:
            combined = (stdout + "\n" + stderr).lower()
            if any(m in combined for m in self._DAEMON_DOWN_MARKERS):
                return None, {
                    "success": False,
                    "error": (
                        "Docker daemon 不可达：守护进程可能未运行，"
                        "或当前用户无权限连接（npipe / unix socket）"
                    ),
                    "error_detail": self._clip(stderr or stdout, self.MAX_ERROR_DETAIL_CHARS),
                }
            return None, {
                "success": False,
                "error": f"docker 命令失败（exit={rc}）",
                "error_detail": self._clip(stderr or stdout, self.MAX_ERROR_DETAIL_CHARS),
            }
        return (rc, stdout, stderr), None

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        text = text or ""
        if len(text) <= limit:
            return text
        return text[:limit] + "…[截断]"

    def _list_containers(self, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        limit_raw = params.get("limit")
        if isinstance(limit_raw, bool) or not isinstance(limit_raw, int):
            limit = self.DEFAULT_LIMIT
        else:
            limit = max(1, min(limit_raw, self.MAX_LIMIT))
        run_result, error = self._exec_readonly(
            ["ps", "-a", "--format", "{{json .}}"], timeout
        )
        if error is not None:
            return error
        _rc, stdout, _stderr = run_result
        containers = []
        truncated = False
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            if len(containers) >= limit:
                truncated = True
                break
            containers.append(
                {
                    "id": raw.get("ID"),
                    "name": raw.get("Names"),
                    "image": raw.get("Image"),
                    "state": raw.get("State"),
                    "status": raw.get("Status"),
                    "ports": self._clip(str(raw.get("Ports") or ""), 300),
                    "created_at": raw.get("CreatedAt"),
                }
            )
        return {
            "success": True,
            "action": "list_containers",
            "containers": containers,
            "container_count": len(containers),
            "truncated": truncated,
            "note": "已省略 Labels / Command / Mounts 等字段（可能含敏感信息或噪音）",
        }

    def _inspect_container(self, container: str, timeout: float) -> dict[str, Any]:
        run_result, error = self._exec_readonly(["inspect", container], timeout)
        if error is not None:
            return error
        _rc, stdout, _stderr = run_result
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return {"success": False, "error": "docker inspect 输出不是合法 JSON"}
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            return {"success": False, "error": f"未找到容器或输出结构异常：{container}"}
        info = data[0]
        state = info.get("State") if isinstance(info.get("State"), dict) else {}
        health = state.get("Health") if isinstance(state.get("Health"), dict) else None
        host_config = (
            info.get("HostConfig") if isinstance(info.get("HostConfig"), dict) else {}
        )
        bindings = (
            host_config.get("PortBindings")
            if isinstance(host_config.get("PortBindings"), dict)
            else {}
        )
        ports = []
        for container_port, host_list in list(bindings.items())[:30]:
            if isinstance(host_list, list) and host_list:
                for binding in host_list[:10]:
                    if isinstance(binding, dict):
                        ports.append(
                            f"{container_port} -> "
                            f"{binding.get('HostIp', '?')}:{binding.get('HostPort', '?')}"
                        )
            else:
                ports.append(f"{container_port} -> (未发布)")
        return {
            "success": True,
            "action": "inspect_container",
            "container": container,
            "id": info.get("Id"),
            "name": (info.get("Name") or "").lstrip("/"),
            "image": info.get("Image"),
            "created": info.get("Created"),
            "state": {
                "status": state.get("Status"),
                "running": state.get("Running"),
                "paused": state.get("Paused"),
                "restarting": state.get("Restarting"),
                "exit_code": state.get("ExitCode"),
                "started_at": state.get("StartedAt"),
                "finished_at": state.get("FinishedAt"),
                "restart_count": info.get("RestartCount"),
            },
            "health": (
                {
                    "status": health.get("Status"),
                    "failing_streak": health.get("FailingStreak"),
                }
                if health
                else None
            ),
            "port_bindings": ports,
            "excluded_for_safety": [
                "Env",
                "Labels",
                "Cmd",
                "Entrypoint",
                "Args",
                "Health.Log",
                "Mounts",
            ],
            "note": "敏感字段已排除，不返回原始 inspect 对象；Health.Log 可能含应用输出，一律不返回",
        }

    def _logs(
        self, container: str, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        tail_raw = params.get("tail")
        if isinstance(tail_raw, bool) or not isinstance(tail_raw, int):
            tail = self.DEFAULT_TAIL
        else:
            tail = max(1, min(tail_raw, self.MAX_TAIL))
        run_result, error = self._exec_readonly(
            ["logs", "--tail", str(tail), "--timestamps", container], timeout
        )
        if error is not None:
            return error
        _rc, stdout, _stderr = run_result
        redacted_total = 0
        text = stdout
        for pattern, repl in _REDACTION_RULES:
            text, count = pattern.subn(repl, text)
            redacted_total += count
        lines = []
        total_chars = 0
        truncated = False
        for line in text.splitlines():
            clipped = self._clip(line, self.MAX_LINE_CHARS)
            if total_chars + len(clipped) > self.MAX_OUTPUT_CHARS:
                truncated = True
                break
            lines.append(clipped)
            total_chars += len(clipped)
        return {
            "success": True,
            "action": "logs",
            "container": container,
            "tail": tail,
            "lines": lines,
            "line_count": len(lines),
            "truncated": truncated,
            "redaction": {
                "best_effort": True,
                "matches_redacted": redacted_total,
                "note": "按常见凭据模式尽力脱敏（password/token/Bearer/AKIA/私钥），不保证覆盖全部敏感信息",
            },
        }
