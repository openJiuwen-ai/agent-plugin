# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HTTP 接口探测工具：仅限本机 localhost 的 GET/HEAD 调试。

继承 ``openjiuwen.core.foundation.tool.Tool``，基于标准库 urllib：
- 仅允许 http/https 且主机为 localhost / 127.0.0.1 / [::1]；
- 拒绝 URL 内嵌凭据、拒绝 POST/上传、不发送任何 cookie / 认证头；
- 不跟随重定向（记录 Location）、忽略环境代理；
- 读取字节与时间有界；
- 支持显式的期望状态码与 JSON 字段断言，断言失败 success=false；
  无断言时仅返回观测结果并标记 verified=false；
- 绝不执行返回内容。
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from openjiuwen.core.foundation.tool import Tool, ToolCard

_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
DEFAULT_MAX_BYTES = 65536
MAX_BYTES_LIMIT = 1048576
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 30.0
PREVIEW_CHARS = 2000
MAX_ASSERTIONS = 20


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """禁用重定向跟随：3xx 直接作为响应报告。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _json_equal(expected: Any, actual: Any) -> bool:
    if isinstance(expected, bool) != isinstance(actual, bool):
        return False
    return expected == actual


def _lookup_path(data: Any, path: str) -> tuple[bool, Any]:
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            if part in current:
                current = current[part]
            else:
                return False, None
        elif isinstance(current, list):
            try:
                index = int(part)
            except ValueError:
                return False, None
            if 0 <= index < len(current):
                current = current[index]
            else:
                return False, None
        else:
            return False, None
    return True, current


class HttpApiProbeTool(Tool):
    """本机 HTTP 接口探测：GET/HEAD + 状态码 / JSON 字段断言。"""

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id="http_api_probe",
                name="http_api_probe",
                description=(
                    "HTTP 接口探测工具（仅限本机）：对 localhost / 127.0.0.1 / [::1] "
                    "发起 GET 或 HEAD 请求，返回状态码、内容类型、耗时与有界响应体预览；"
                    "支持显式的期望状态码与 JSON 字段断言（断言失败 success=false），"
                    "无断言时仅报告观测结果（verified=false，不代表业务成功）。"
                    "不发送 cookie / 认证头，不跟随重定向（记录 Location），"
                    "忽略环境代理，读取字节与时间有界，绝不执行返回内容。"
                    "调试本地 Web 服务 / API 时调用。"
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": (
                                "目标 URL：仅 http/https 且主机为 localhost / 127.0.0.1 / [::1]，"
                                "不得内嵌用户名密码"
                            ),
                        },
                        "method": {
                            "type": "string",
                            "enum": ["GET", "HEAD"],
                            "description": "HTTP 方法（默认 GET；仅允许 GET/HEAD）",
                        },
                        "expected_status": {
                            "type": "integer",
                            "description": "期望的状态码断言（可选，如 200）",
                        },
                        "expected_json": {
                            "type": "array",
                            "description": (
                                "JSON 字段断言数组（可选），每项 {\"field\": \"a.b\", \"value\": ...}，"
                                "field 支持点号路径与数组下标"
                            ),
                        },
                        "max_bytes": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 1048576,
                            "description": "响应体最大读取字节数（默认 65536，超出截断）",
                        },
                        "timeout": {
                            "type": "number",
                            "minimum": 1,
                            "maximum": 30,
                            "description": "请求超时秒数（默认 10）",
                        },
                    },
                    "required": ["url"],
                },
            )
        )

    async def invoke(self, inputs: Any, **kwargs: Any) -> dict[str, Any]:
        params = self._extract_params(inputs)
        url = params.get("url")
        if not isinstance(url, str) or not url.strip():
            return {"success": False, "error": "缺少必填参数 url"}
        method = params.get("method", "GET")
        if not isinstance(method, str) or method.upper() not in ("GET", "HEAD"):
            return {
                "success": False,
                "error": f"方法仅允许 GET / HEAD（当前：{method!r}）；POST / 上传不在本工具范围",
            }
        method = method.upper()
        error = self._validate_url(url)
        if error is not None:
            return {"success": False, "error": error}
        expected_status = params.get("expected_status")
        if expected_status is not None and (
            isinstance(expected_status, bool)
            or not isinstance(expected_status, int)
            or not 100 <= expected_status <= 599
        ):
            return {"success": False, "error": "expected_status 必须是 100-599 的整数"}
        expected_json, json_error = self._normalize_expected_json(
            params.get("expected_json")
        )
        if json_error is not None:
            return {"success": False, "error": json_error}
        max_bytes = self._bounded_int(params.get("max_bytes"), DEFAULT_MAX_BYTES, 1, MAX_BYTES_LIMIT)
        timeout = self._bounded_number(params.get("timeout"), DEFAULT_TIMEOUT_SECONDS, 1.0, MAX_TIMEOUT_SECONDS)
        return self._probe(
            url, method, expected_status, expected_json, max_bytes, timeout
        )

    async def stream(self, inputs: Any, **kwargs: Any):
        yield await self.invoke(inputs, **kwargs)

    @staticmethod
    def _extract_params(inputs: Any) -> dict[str, Any]:
        keys = ("url", "method", "expected_status", "expected_json", "max_bytes", "timeout")
        if isinstance(inputs, dict):
            return {k: inputs[k] for k in keys if k in inputs}
        result: dict[str, Any] = {}
        for k in keys:
            v = getattr(inputs, k, None)
            if v is not None:
                result[k] = v
        return result

    @staticmethod
    def _validate_url(url: str) -> str | None:
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            return "URL 无法解析（端口非法）"
        if parts.scheme.lower() not in ("http", "https"):
            return f"仅允许 http / https 协议（当前：{parts.scheme or '(空)'}）"
        if parts.username is not None or parts.password is not None:
            return "URL 中包含用户名 / 密码，已拒绝（本工具不发送任何凭据）"
        host = (parts.hostname or "").lower()
        if host not in _ALLOWED_HOSTS:
            return (
                f"仅允许本机地址 localhost / 127.0.0.1 / [::1]（当前主机：{host or '(空)'}）；"
                "外网与内网其他主机不在本工具范围"
            )
        if port is not None and not 1 <= port <= 65535:
            return f"端口非法：{port}"
        return None

    def _normalize_expected_json(self, raw: Any) -> tuple[list[dict[str, Any]] | None, str | None]:
        if raw is None:
            return None, None
        if not isinstance(raw, list) or not raw:
            return None, "expected_json 必须是非空数组：[{\"field\": \"a.b\", \"value\": ...}]"
        if len(raw) > MAX_ASSERTIONS:
            return None, f"断言数量超限（>{MAX_ASSERTIONS}）"
        for item in raw:
            if not isinstance(item, dict) or "field" not in item or "value" not in item:
                return None, "每个断言必须包含 field 与 value"
            if not isinstance(item["field"], str) or not item["field"].strip():
                return None, "断言 field 必须是非空字符串（点号路径）"
        return raw, None

    @staticmethod
    def _bounded_int(value: Any, default: int, low: int, high: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            return default
        return max(low, min(value, high))

    @staticmethod
    def _bounded_number(value: Any, default: float, low: float, high: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        return max(low, min(float(value), high))

    def _probe(
        self,
        url: str,
        method: str,
        expected_status: int | None,
        expected_json: list[dict[str, Any]] | None,
        max_bytes: int,
        timeout: float,
    ) -> dict[str, Any]:
        opener = urllib.request.build_opener(
            _NoRedirectHandler, urllib.request.ProxyHandler({})
        )
        request = urllib.request.Request(url, method=method)
        started = time.monotonic()
        try:
            resp = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            resp = exc
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(reason).lower():
                return {
                    "success": False,
                    "error": f"请求超时（{timeout:.0f} 秒）：{url}",
                }
            return {"success": False, "error": f"连接失败：{reason}"}
        except (TimeoutError, socket.timeout):
            return {"success": False, "error": f"请求超时（{timeout:.0f} 秒）：{url}"}
        except ValueError as exc:
            return {"success": False, "error": f"URL 无法解析：{exc}"}
        elapsed_ms = int((time.monotonic() - started) * 1000)
        body = b""
        truncated = False
        with resp:
            status = resp.getcode()
            headers = resp.headers
            while len(body) <= max_bytes:
                chunk = resp.read(min(8192, max_bytes + 1 - len(body)))
                if not chunk:
                    break
                body += chunk
            if len(body) > max_bytes:
                body = body[:max_bytes]
                truncated = True
        body_text = body.decode("utf-8", "replace")
        preview = body_text[:PREVIEW_CHARS]
        redirect = 300 <= (status or 0) < 400
        assertions: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        if expected_status is not None:
            ok = status == expected_status
            entry = {
                "assertion": "status",
                "expected": expected_status,
                "actual": status,
                "pass": ok,
            }
            assertions.append(entry)
            if not ok:
                failures.append(entry)
        if expected_json:
            parsed = None
            parse_failed = None
            if truncated:
                parse_failed = "响应体被截断，无法可靠解析 JSON"
            else:
                try:
                    parsed = json.loads(body_text)
                except json.JSONDecodeError as exc:
                    parse_failed = f"响应体不是合法 JSON：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）"
            if parse_failed is not None:
                entry = {
                    "assertion": "json",
                    "reason": parse_failed,
                    "pass": False,
                }
                failures.append(entry)
            else:
                for spec in expected_json:
                    found, actual = _lookup_path(parsed, spec["field"])
                    ok = found and _json_equal(spec["value"], actual)
                    entry = {
                        "assertion": "json_field",
                        "field": spec["field"],
                        "expected": spec["value"],
                        "actual": actual if found else "<缺失>",
                        "pass": ok,
                    }
                    assertions.append(entry)
                    if not ok:
                        failures.append(entry)
        has_assertions = expected_status is not None or bool(expected_json)
        result: dict[str, Any] = {
            "success": not failures,
            "verified": has_assertions and not failures,
            "action": "probe",
            "url": url,
            "method": method,
            "status_code": status,
            "content_type": headers.get("Content-Type") or None,
            "content_length_header": headers.get("Content-Length"),
            "elapsed_ms": elapsed_ms,
            "body_bytes": len(body),
            "body_truncated": truncated,
            "body_preview": preview,
            "assertions": assertions,
            "assertion_failures": failures,
            "redirect": (
                {"followed": False, "location": headers.get("Location")}
                if redirect
                else None
            ),
            "safety": {
                "allowed_hosts": sorted(_ALLOWED_HOSTS),
                "credentials_sent": False,
                "proxies_ignored": True,
                "response_never_executed": True,
            },
        }
        if not has_assertions:
            result["note"] = "未提供断言：以上仅为观测结果（verified=false），不代表业务成功"
        return result
