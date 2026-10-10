---
name: http-api-debugging
description: 本机 HTTP 接口的只读探测与断言。当用户想确认本地服务是否在线、接口返回什么、或验证返回的状态码 / JSON 字段是否符合预期时使用；配合 http_api_probe 工具，仅 GET/HEAD、仅 localhost、绝不执行响应内容。
---

# HTTP 接口探测（http-api-debugging）

## 目标

以可断言、可复核的方式回答：本地接口现在返回什么（状态码 / 内容类型 / 耗时 / 有界响应预览），以及是否符合预期（状态码与 JSON 字段断言）。

## 工作流

1. 确认目标是本机服务（http(s)://localhost / 127.0.0.1 / [::1]）；非本机地址会被工具拒绝，属设计行为。
2. `http_api_probe` 先做观测（不传断言）：看 status_code、content_type、elapsed_ms、body_preview；3xx 会记录 redirect.location 且不跟随。
3. 需要验证时补断言：expected_status 与 expected_json（field 支持点号嵌套与数组下标）；断言失败 → success=false 并给出 expected/actual 对照。
4. 结果解读：verified=false 且无断言 = 仅观测，不代表业务成功；body_truncated=true = 响应被截断，JSON 断言不可靠；响应内容永远不被执行。

## 验收标准

- 未发起任何非 GET/HEAD 请求；未发送 cookie / 认证头；未跟随重定向；未使用环境代理。
- 断言结果逐条可复核（assertions 列表含 expected/actual/pass）。
- 连接失败 / 超时返回明确 success=false 与原因。
