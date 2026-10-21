---
name: agent-health-check
description: 三方 Agent/智能体 CLI 健康诊断工作流：当用户要求体检本机 codex、Claude Code、Kimi Code、Zcode 等工具，或排查某个 Agent 不可用、未登录、连不通时，调用 agent_health_check 工具采集状态并按本流程解读与给出修复建议。
---

# 三方 Agent 健康诊断

## 目标

对本机第三方 Agent / 智能体 CLI 做只读体检，输出一份结构清晰、可行动的健康报告。

- 覆盖安装状态、版本、配置/登录凭据、（可选）API 端点连通性
- 每个问题必须附带具体修复建议，不能只报状态不给方案

## 工作流

### 1. 采集状态

调用 `agent_health_check` 工具：

- 用户点名具体工具（如「看看 codex 和 claude 能不能用」）→ 传 `targets`，如 `["codex", "claudecode"]`
- 用户要求全面体检 → 省略 `targets`，检测全部已知 Agent
- 用户提到「连不上 / 超时 / 网络问题」，或前一次检测发现已安装但状态异常 → 设 `check_network: true`
- 目标名对照：`codex`→codex，`claude`/`claude code`→claudecode，`kimi`→kimicode，`zcode`→zcode

### 2. 解读结果

逐项按 `status` 分级：

- `healthy`：已安装、版本可读、配置或凭据存在
- `degraded`：已安装但存在问题——逐个看 `issues` 字段定位原因
- `missing`：PATH 中找不到命令，按 `hints` 给出安装指引
- `unknown_targets` 非空：用户点名的目标不在已知清单，说明并询问是否指的是其他工具

### 3. 给出修复建议

按问题类型给出具体动作：

- 未安装 → 引用该 Agent 的 `install_hint`
- 版本探测失败 → 可能是 shim/启动脚本损坏，建议重装或检查 PATH 指向（报告 `executable` 路径）
- 未发现配置/凭据 → 提示执行该工具的登录命令或设置对应 API Key 环境变量（列出 `env_keys` 候选名，绝不展示密钥值）
- 网络不通 → 建议检查代理（如 `HTTPS_PROXY`）、防火墙、DNS，或确认服务端可用性

### 4. 输出健康报告

## 输出要求

- 先给一行总体结论（引用 `summary.overall`），再按 Agent 逐项列出：状态图标（✅/⚠️/❌）、版本、问题、建议
- 报告必须基于工具返回数据，不得臆测未检测的项
- 检测是只读的；如需执行修复（安装、登录、改配置），先征得用户确认再操作

## 决策规则

- 优先只读检测；除非用户明确要求，不要主动执行安装或修改配置
- 网络探测可能受代理影响，`reachable: false` 不必然代表服务不可用——表述为「本机到该端点 TCP 连接失败」
