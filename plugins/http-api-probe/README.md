# HTTP API Probe / HTTP 接口探测助手

> **一句话定位（zh）**：探测本机 localhost HTTP 接口——GET/HEAD、状态码、耗时与有界响应体预览，支持状态码与 JSON 字段断言，不跟随重定向、不走代理、绝不执行响应内容。
>
> **One-liner (en)**: Probe localhost HTTP APIs with GET/HEAD — status, timing and bounded body previews, with status-code and JSON field assertions; redirects not followed, proxies ignored, response content never executed.

## 核心能力

- **观测**：`status_code` / `content_type` / `elapsed_ms` / `body_preview`（默认最多读取 64KB，上限 1MB，超出显式标记 `body_truncated`）。
- **断言**：`expected_status`（状态码）与 `expected_json`（`[{field, value}]`，field 支持 `data.items.0.id` 式嵌套与数组下标）；逐条返回 `assertions`（expected/actual/pass），失败 → `success=false` + `assertion_failures`。
- **无断言 = 仅观测**：返回 `verified=false` 与明确提示，不冒充业务成功。
- **首版安全边界**：仅 `http(s)://localhost` / `127.0.0.1` / `[::1]`；拒绝 URL 内嵌用户名密码；仅 GET/HEAD（无 POST/上传/任意 shell）；**不跟随重定向**（3xx 记录 `redirect_to`）；**忽略环境代理**；不发送 cookie / 认证头；绝不执行响应内容。
- 连接失败、超时（默认 10s，上限 30s）返回明确 `success=false`。

## 组件协作

```
用户请求（本地服务在线？返回什么？字段对不对？）
   │
   ▼
skills/http-api-debugging ── 观测→断言的工作流与结果解读规则
   │
   ▼
tools/http_api_probe_tool.py ── HttpApiProbeTool
   ├── 校验（仅 localhost / GET / HEAD / 无凭据）
   ├── 请求（urllib + 无代理 + 不跟随重定向 + 有界读取）
   └── 断言（expected_status / expected_json → assertions + failures）
```

本插件不包含 rail。

## 依赖说明

- 仅 Python 标准库（urllib），无必装第三方依赖。
- 可选依赖：无（未来如需 HTTP/2 等能力可考虑可选 httpx，当前版本不引入）。

## 目录结构

```
http-api-probe/
├── manifest.json
├── README.md
├── LICENSE                    # Apache-2.0（与仓库根一致）
├── PROVENANCE.md
├── skills/http-api-debugging/SKILL.md
└── tools/http_api_probe_tool.py
```
