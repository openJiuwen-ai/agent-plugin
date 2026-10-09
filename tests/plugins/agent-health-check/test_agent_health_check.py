# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""agent-health-check 行为测试：真实导入 openjiuwen 运行时并执行 tool。

无 openjiuwen 环境时整体跳过（CI 的 hot-load 节点负责全量执行）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

pytest.importorskip("openjiuwen")

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL_PATH = REPO_ROOT / "plugins" / "agent-health-check" / "tools" / "agent_health_tool.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool_module():
    return _load_module(TOOL_PATH, "agent_health_tool_under_test")


def test_full_scan_returns_structured_report(tool_module):
    tool = tool_module.AgentHealthCheckTool()
    result = asyncio.run(tool.invoke({}))
    assert result["success"] is True
    assert result["summary"]["total"] == len(tool_module.KNOWN_AGENTS)
    assert len(result["items"]) == len(tool_module.KNOWN_AGENTS)
    for item in result["items"]:
        assert item["status"] in {"healthy", "degraded", "missing", "error"}
    assert result["options"]["check_network"] is False


def test_targets_filter_and_unknown(tool_module):
    tool = tool_module.AgentHealthCheckTool()
    result = asyncio.run(tool.invoke({"targets": ["codex", "no-such-agent"]}))
    assert result["success"] is True
    assert [item["id"] for item in result["items"]] == ["codex"]
    assert result["unknown_targets"] == ["no-such-agent"]


def test_string_json_input_accepted(tool_module):
    tool = tool_module.AgentHealthCheckTool()
    result = asyncio.run(tool.invoke(json.dumps({"targets": ["claudecode"]})))
    assert result["success"] is True
    assert [item["id"] for item in result["items"]] == ["claudecode"]


def test_invalid_input_fails_open(tool_module):
    tool = tool_module.AgentHealthCheckTool()
    result = asyncio.run(tool.invoke(["not-a-dict"]))
    assert result["success"] is False
    assert "error" in result


def test_timeout_is_clamped(tool_module):
    tool = tool_module.AgentHealthCheckTool()
    result = asyncio.run(tool.invoke({"targets": ["codex"], "timeout_seconds": 999}))
    assert result["success"] is True
    assert result["options"]["timeout_seconds"] == 30.0


def test_missing_agent_reports_install_hint(tool_module):
    tool = tool_module.AgentHealthCheckTool()
    fake_spec = {
        "id": "definitely-not-installed-agent",
        "name": "Definitely Not Installed",
        "commands": ["definitely-not-installed-agent-cli"],
        "version_args": ["--version"],
        "config_dirs": [],
        "config_files": [],
        "env_keys": [],
        "endpoint_host": None,
        "install_hint": "install it",
    }
    item = tool_module._check_agent(fake_spec, timeout=1.0, check_network=False)
    assert item["status"] == "missing"
    assert item["installed"] is False
    assert item["hints"] == ["install it"]
