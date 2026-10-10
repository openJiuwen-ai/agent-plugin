# SPDX-License-Identifier: Apache-2.0
"""Run explicit browser steps in a fresh context and keep screenshot evidence."""
from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from openjiuwen.core.foundation.tool import Tool, ToolCard


def local_url(value):
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return (parsed.scheme in ("http", "https") and parsed.hostname in ("localhost", "127.0.0.1", "::1")
            and not parsed.username and not parsed.password)


def validate_steps(inputs):
    if not local_url(inputs.get("url")):
        raise ValueError("First version accepts only localhost/127.0.0.1/[::1] HTTP(S) pages")
    steps = inputs.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 20:
        raise ValueError("Provide 1-20 explicit steps")
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("Each step must be an object")
        action = step.get("action")
        if action not in ("click", "fill", "assert_visible", "assert_text"):
            raise ValueError("Unsupported step action")
        if not isinstance(step.get("selector"), str) or not 1 <= len(step["selector"]) <= 500:
            raise ValueError("Each step requires a selector of 1-500 characters")
        if action in ("fill", "assert_text") and (not isinstance(step.get("text"), str) or len(step["text"]) > 5000):
            raise ValueError("fill/assert_text requires text of at most 5000 characters")
    if not isinstance(inputs.get("output_dir"), str) or not inputs["output_dir"].strip():
        raise ValueError("output_dir is required")
    if type(inputs.get("timeout_ms", 5000)) is not int or not 100 <= inputs.get("timeout_ms", 5000) <= 15000:
        raise ValueError("timeout_ms must be 100-15000")
    if not any(s["action"].startswith("assert_") for s in steps):
        raise ValueError("At least one explicit assertion is required for an acceptance result")
    return steps


async def run_page(inputs):
    steps = validate_steps(inputs)
    try:
        from playwright.async_api import async_playwright, expect, Error as BrowserError
    except ImportError:
        return {"success": False, "error": "missing_dependency", "dependency": "playwright"}
    # A per-run subdirectory prevents concurrent sessions overwriting evidence.
    root = Path(inputs["output_dir"]).expanduser().resolve()
    output = root / ("page-check-" + uuid.uuid4().hex)
    output.mkdir(parents=True, exist_ok=False)
    results, browser_errors = [], []
    report = {"success": False, "passed": False, "steps": results,
              "browser_errors": browser_errors, "output_dir": str(output),
              "note": "Only explicit assertions determine acceptance; console errors are supplementary evidence. No login state is reused."}
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context(accept_downloads=False, service_workers="block")

                async def route_request(route):
                    if local_url(route.request.url):
                        await route.continue_()
                    else:
                        await route.abort()

                await context.route("**/*", route_request)
                # Prevent non-HTTP channels escaping the local-page scope.
                await context.route_web_socket("**/*", lambda ws: ws.close())
                page = await context.new_page()
                page.set_default_timeout(inputs.get("timeout_ms", 5000))
                page.on("pageerror", lambda error: browser_errors.append(str(error)[:2000]) if len(browser_errors) < 20 else None)
                page.on("console", lambda msg: browser_errors.append(msg.text[:2000]) if msg.type == "error" and len(browser_errors) < 20 else None)
                response = await page.goto(inputs["url"], wait_until="domcontentloaded", timeout=15000)
                if response and response.status >= 400:
                    raise ValueError(f"Initial page returned HTTP {response.status}")
                for index, step in enumerate(steps, 1):
                    record = {"index": index, "action": step["action"], "selector": step["selector"], "passed": False}
                    results.append(record)
                    try:
                        target = page.locator(step["selector"])
                        action = step["action"]
                        if action == "click":
                            await target.click()
                        elif action == "fill":
                            await target.fill(step["text"])
                        elif action == "assert_visible":
                            await expect(target).to_be_visible(timeout=inputs.get("timeout_ms", 5000))
                        else:
                            await expect(target).to_contain_text(step["text"], timeout=inputs.get("timeout_ms", 5000))
                        record["passed"] = True
                    except (BrowserError, AssertionError) as exc:
                        record["error"] = str(exc)[:2000]
                    shot = output / f"step-{index:02}.png"
                    await page.screenshot(path=str(shot), full_page=False, timeout=10000)
                    record["screenshot"] = str(shot)
                    if not record["passed"]:
                        break
                report["passed"] = len(results) == len(steps) and all(r["passed"] for r in results)
                report["success"] = report["passed"]
                if not report["passed"]:
                    report["error"] = "acceptance_failed"
            finally:
                await browser.close()
    except (BrowserError, ValueError, AttributeError) as exc:
        report["error"] = str(exc)[:2000]
        report["setup_help"] = "Install playwright>=1.48 and run python -m playwright install chromium in the Jiuwen runtime."
    report_path = output / "report.json"
    report["report_path"] = str(report_path)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


class PageAcceptanceTool(Tool):
    def __init__(self) -> None:
        super().__init__(ToolCard(id="page_acceptance", name="page_acceptance",
            description="在新 Chromium 会话中验收本地页面，按给定步骤操作、断言和截图；可能触发页面数据修改。",
            input_params={"type": "object", "properties": {
                "url": {"type": "string"}, "output_dir": {"type": "string"},
                "timeout_ms": {"type": "integer", "minimum": 100, "maximum": 15000},
                "steps": {"type": "array", "minItems": 1, "maxItems": 20, "items": {
                    "type": "object", "properties": {
                        "action": {"type": "string", "enum": ["click", "fill", "assert_visible", "assert_text"]},
                        "selector": {"type": "string"}, "text": {"type": "string"}},
                    "required": ["action", "selector"]}}}, "required": ["url", "output_dir", "steps"]}))

    async def invoke(self, inputs, **kwargs):
        try:
            if not isinstance(inputs, dict):
                raise ValueError("Expected an object")
            return await asyncio.wait_for(run_page(inputs), timeout=420)
        except (ValueError, TypeError, OSError, asyncio.TimeoutError) as exc:
            return {"success": False, "passed": False, "error": str(exc) or "page_check_timeout"}

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)
