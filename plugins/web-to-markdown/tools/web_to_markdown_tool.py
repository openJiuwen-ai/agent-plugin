# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""网页转 Markdown 工具。

继承 ``openjiuwen.core.foundation.tool.Tool``，通过 ``ToolCard`` 声明能力。
纯标准库实现（``urllib`` + ``html.parser`` + ``ipaddress`` + ``socket``）：
- ``import`` 期无副作用；仅在 ``invoke`` 内联网抓取。
- 只返回 Markdown 文本，不写文件、不注入运行时对象、无第三方依赖。
- SSRF 防护：解析主机名后拒绝回环/私网/链路本地等地址，并逐跳复检重定向目标。
- 响应体限长：读取不超过 2MB，并校验 Content-Type，避免把二进制大文件读入内存。
"""

from __future__ import annotations

import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

from openjiuwen.core.foundation.tool import Tool, ToolCard

_DEFAULT_TIMEOUT = 15
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; JiuwenSwarm/1.0; +https://openjiuwen.com)"
)
_URL_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2MB，与 scripts/publish.py 的 MAX_RESPONSE_BYTES 一致
_ALLOWED_CONTENT_TYPES = {
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "application/xml",
    "text/xml",
}


class _SSRFBlocked(Exception):
    """目标地址命中内网/回环/链路本地等禁止访问范围，拒绝请求。"""


def _is_forbidden_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """判断 IP 是否属于禁止访问范围（SSRF 防护）。"""
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _validate_host(host: str) -> None:
    """解析主机名得到全部 IP，任一命中禁止范围即拒绝（含 DNS 多地址场景）。"""
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise _SSRFBlocked(f"主机名解析失败：{host}") from exc
    if not infos:
        raise _SSRFBlocked(f"主机名无解析结果：{host}")
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _is_forbidden_ip(ip):
            raise _SSRFBlocked(f"拒绝访问内网/回环地址：{host} -> {ip}")


def _validate_url_host(url: str) -> None:
    """解析 URL 主机名并做 SSRF 校验。"""
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    if not host:
        raise _SSRFBlocked(f"URL 缺少主机名：{url}")
    _validate_host(host)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """重定向时逐跳复检目标主机，防止通过 3xx 绕过前置 SSRF 校验。"""

    def __init__(self, validator: Any) -> None:
        super().__init__()
        self._validator = validator

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self._validator(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _MarkdownExtractor(HTMLParser):
    """把 HTML 正文转换为 Markdown 的轻量解析器（纯标准库）。

    跳过 ``script`` / ``style`` 等元内容标签，处理标题、段落、链接、
    图片、强调、列表、引用与代码块。
    """

    _SKIP_TAGS = {
        "script",
        "style",
        "noscript",
        "template",
        "head",
    }
    _HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self.title = ""
        self._skip_counts: dict[str, int] = {}
        self._in_pre = False
        self._in_title = False
        self._anchor_href = ""
        self._list_stack: list[str] = []

    def _skipping(self) -> bool:
        return any(self._skip_counts.values())

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
            return
        if tag == "body":
            # <body> 是正文起点：清空可能因 <head> 等未闭合而残留的 skip 计数
            self._skip_counts.clear()
            return
        if tag in self._SKIP_TAGS:
            self._skip_counts[tag] = self._skip_counts.get(tag, 0) + 1
            return
        if self._skipping():
            return
        attr_map = {name: value for name, value in attrs}
        if tag == "pre":
            self._in_pre = True
            self._out.append("\n\n```\n")
        elif tag == "code":
            if not self._in_pre:
                self._out.append("`")
        elif tag == "a":
            self._anchor_href = attr_map.get("href") or ""
            self._out.append("[")
        elif tag == "img":
            src = attr_map.get("src") or ""
            alt = attr_map.get("alt") or ""
            self._out.append(f"\n\n![{alt}]({src})\n\n")
        elif tag in ("strong", "b"):
            self._out.append("**")
        elif tag in ("em", "i"):
            self._out.append("*")
        elif tag in self._HEADINGS:
            level = int(tag[1])
            self._out.append("\n\n" + "#" * level + " ")
        elif tag == "li":
            self._out.append("\n- ")
        elif tag in ("ul", "ol"):
            self._list_stack.append(tag)
            self._out.append("\n")
        elif tag == "blockquote":
            self._out.append("\n\n> ")
        elif tag == "br":
            self._out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            return
        if tag in self._SKIP_TAGS:
            count = self._skip_counts.get(tag, 0)
            if count > 0:
                self._skip_counts[tag] = count - 1
            return
        if self._skipping():
            return
        if tag == "pre":
            self._in_pre = False
            self._out.append("\n```\n\n")
        elif tag == "code":
            if not self._in_pre:
                self._out.append("`")
        elif tag == "a":
            href = self._anchor_href
            self._anchor_href = ""
            if href and not href.startswith("#"):
                self._out.append(f"]({href})")
            else:
                self._out.append("]")
        elif tag in ("strong", "b"):
            self._out.append("**")
        elif tag in ("em", "i"):
            self._out.append("*")
        elif tag in self._HEADINGS:
            self._out.append("\n\n")
        elif tag == "li":
            self._out.append("\n")
        elif tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
            self._out.append("\n")
        elif tag == "blockquote":
            self._out.append("\n\n")
        elif tag in ("p", "div", "section", "article", "main"):
            self._out.append("\n\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skipping():
            return
        self._out.append(data)

    def result(self) -> str:
        text = "".join(self._out)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


class WebToMarkdownTool(Tool):
    """抓取指定网页正文并转换为 Markdown 文本。"""

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id="web_to_markdown",
                name="web_to_markdown",
                description=(
                    "网页转 Markdown 工具：给定 url，抓取网页正文并返回 title 与 markdown。"
                    "当用户要求把网页、链接或在线文章转成 Markdown 笔记、保存为 .md 文件"
                    "或整理进知识库时调用，需配合 web-to-markdown skill 使用。"
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "要抓取并转换的完整网页地址（含 http:// 或 https://）",
                        },
                        "timeout_seconds": {
                            "type": "integer",
                            "description": "抓取超时秒数，默认 15",
                        },
                    },
                    "required": ["url"],
                },
            )
        )

    async def invoke(self, inputs: Any, **kwargs: Any) -> dict[str, Any]:
        url = self._extract_url(inputs)
        if not url:
            return {
                "success": False,
                "error": "缺少必填参数 url：请以 {'url': '<网页地址>'} 形式传入",
            }
        timeout = self._extract_timeout(inputs)
        try:
            html = self._fetch(url, timeout)
        except _SSRFBlocked as exc:
            return {"success": False, "error": f"安全校验失败：{exc}"}
        except urllib.error.HTTPError as exc:
            return {"success": False, "error": f"HTTP 错误 {exc.code}：{url}"}
        except urllib.error.URLError as exc:
            return {"success": False, "error": f"网络请求失败：{exc.reason}"}
        except Exception as exc:  # noqa: BLE001 - 兜底网络异常，如实上报
            return {"success": False, "error": f"抓取失败：{exc}"}

        extractor = _MarkdownExtractor()
        extractor.feed(html)
        markdown = extractor.result()
        title = extractor.title.strip() or self._fallback_title(url)

        if not markdown:
            return {"success": False, "error": f"网页正文为空，未生成 Markdown：{url}"}

        return {
            "success": True,
            "url": url,
            "title": title,
            "markdown": markdown,
        }

    async def stream(self, inputs: Any, **kwargs: Any):
        yield await self.invoke(inputs, **kwargs)

    @staticmethod
    def _extract_url(inputs: Any) -> str:
        if isinstance(inputs, dict):
            raw = inputs.get("url")
        else:
            raw = getattr(inputs, "url", None)
        if not isinstance(raw, str):
            return ""
        raw = raw.strip()
        return raw if _URL_SCHEME_RE.match(raw) else ""

    @staticmethod
    def _extract_timeout(inputs: Any) -> int:
        raw = None
        if isinstance(inputs, dict):
            raw = inputs.get("timeout_seconds")
        elif inputs is not None:
            raw = getattr(inputs, "timeout_seconds", None)
        if isinstance(raw, bool) or not isinstance(raw, int):
            return _DEFAULT_TIMEOUT
        return max(1, min(raw, 120))

    @staticmethod
    def _fetch(url: str, timeout: int) -> str:
        _validate_url_host(url)

        opener = urllib.request.build_opener(_SafeRedirectHandler(_validate_url_host))
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": _DEFAULT_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        with opener.open(request, timeout=timeout) as response:
            content_type = response.headers.get_content_type()
            if content_type and content_type.lower() not in _ALLOWED_CONTENT_TYPES:
                raise ValueError(f"不支持的 Content-Type：{content_type}")
            data = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(data) > _MAX_RESPONSE_BYTES:
                raise ValueError(
                    f"响应体超过 {_MAX_RESPONSE_BYTES // (1024 * 1024)}MB 上限"
                )
        charset = response.headers.get_content_charset() or "utf-8"
        return data.decode(charset, errors="replace")

    @staticmethod
    def _fallback_title(url: str) -> str:
        path = url.split("//", 1)[-1].rstrip("/")
        segment = path.split("/")[-1] if "/" in path else path
        return segment or "未命名网页"
