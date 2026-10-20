# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""web-to-markdown 行为测试：真实导入 openjiuwen 运行时并执行 tool。

无 openjiuwen 环境时整体跳过（CI 的 hot-load 节点负责全量执行）。
HTML 转 Markdown 的解析逻辑走纯本地断言，不联网。
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("openjiuwen")

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOL_PATH = (
    REPO_ROOT / "plugins" / "web-to-markdown" / "tools" / "web_to_markdown_tool.py"
)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool_module():
    return _load_module(TOOL_PATH, "web_to_markdown_tool_under_test")


def test_tool_is_real_tool(tool_module):
    from openjiuwen.core.foundation.tool import Tool

    tool = tool_module.WebToMarkdownTool()
    assert isinstance(tool, Tool)


def test_missing_url_degrades(tool_module):
    tool = tool_module.WebToMarkdownTool()
    result = asyncio.run(tool.invoke({}))
    assert result["success"] is False
    assert "url" in result["error"]


def test_invalid_url_degrades(tool_module):
    tool = tool_module.WebToMarkdownTool()
    result = asyncio.run(tool.invoke({"url": "not-a-url"}))
    assert result["success"] is False


def test_extractor_converts_html(tool_module):
    extractor = tool_module._MarkdownExtractor()
    html = (
        "<html><head><title>测试文章</title></head><body>"
        "<h1>标题</h1><p>正文 <a href='https://example.com'>链接</a></p>"
        "<ul><li>第一项</li><li>第二项</li></ul>"
        "</body></html>"
    )
    extractor.feed(html)
    markdown = extractor.result()
    assert extractor.title == "测试文章"
    assert "# 标题" in markdown
    assert "[链接](https://example.com)" in markdown
    assert "- 第一项" in markdown


def test_extractor_skips_script(tool_module):
    extractor = tool_module._MarkdownExtractor()
    html = "<html><body><script>var evil = 'x';</script><p>正文</p></body></html>"
    extractor.feed(html)
    markdown = extractor.result()
    assert "evil" not in markdown
    assert "正文" in markdown


def test_extractor_handles_code_block(tool_module):
    extractor = tool_module._MarkdownExtractor()
    html = "<html><body><pre><code>print('hi')</code></pre></body></html>"
    extractor.feed(html)
    markdown = extractor.result()
    assert "```" in markdown
    assert "print('hi')" in markdown


def test_extractor_unclosed_form_keeps_content(tool_module):
    """未闭合的 <form> 不应吞掉后续正文。"""
    extractor = tool_module._MarkdownExtractor()
    html = "<html><body><form><input name='q'><p>这段正文必须被保留</p></body></html>"
    extractor.feed(html)
    markdown = extractor.result()
    assert "这段正文必须被保留" in markdown


def test_extractor_unclosed_svg_keeps_content(tool_module):
    """未闭合的 <svg> 不应吞掉后续正文。"""
    extractor = tool_module._MarkdownExtractor()
    html = "<html><body><svg><circle r='1'/><p>SVG 后的正文必须被保留</p></body></html>"
    extractor.feed(html)
    markdown = extractor.result()
    assert "SVG 后的正文必须被保留" in markdown


def test_validate_host_rejects_loopback(tool_module):
    with pytest.raises(tool_module._SSRFBlocked):
        tool_module._validate_host("127.0.0.1")


def test_validate_host_rejects_private(tool_module):
    for host in ("10.0.0.1", "172.16.0.1", "192.168.1.1"):
        with pytest.raises(tool_module._SSRFBlocked):
            tool_module._validate_host(host)


def test_validate_host_rejects_cloud_metadata(tool_module):
    with pytest.raises(tool_module._SSRFBlocked):
        tool_module._validate_host("169.254.169.254")


def test_is_forbidden_ip_allows_public(tool_module):
    import ipaddress

    assert tool_module._is_forbidden_ip(ipaddress.ip_address("8.8.8.8")) is False
    assert tool_module._is_forbidden_ip(ipaddress.ip_address("1.1.1.1")) is False
