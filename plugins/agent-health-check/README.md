# 三方Agent健康检测（Agent Health Check）

只读体检本机第三方 AI Agent / 智能体 CLI 工具的健康状态，覆盖安装、版本、登录配置与网络连通性，并输出可行动的修复建议。

## 支持检测的 Agent

| id | 名称 | 检测命令 |
| --- | --- | --- |
| `codex` | Codex CLI (OpenAI) | `codex` |
| `claudecode` | Claude Code (Anthropic) | `claude` |
| `kimicode` | Kimi Code (Moonshot) | `kimi` / `kimi-cli` |
| `zcode` | Z Code | `zcode` / `z-code` |
| `gemini-cli` | Gemini CLI (Google) | `gemini` |
| `cursor-agent` | Cursor Agent CLI | `cursor-agent` / `cursor` |
| `aider` | Aider | `aider` |
| `copilot-cli` | GitHub Copilot CLI | `copilot` / `gh-copilot` |

## 核心能力

- **安装检测**：解析 PATH 中的可执行文件位置
- **版本探测**：运行 `--version`（带超时保护，不弹窗）
- **配置/登录检测**：检查用户主目录下的配置目录与鉴权环境变量是否存在（只看是否存在，不读取密钥内容）
- **连通性探测**（可选）：对已知 API 端点做 TCP:443 连接测试并报告延迟
- **分级报告**：`healthy` / `degraded` / `missing` 三级状态 + 总体结论 + 逐项修复建议

## 包内容

| 组件 | 类型 | 功能 |
| --- | --- | --- |
| `agent_health_check` | Tool | 执行只读健康扫描，返回结构化结果 |
| `agent-health-check` | Skill | 诊断工作流：何时调用工具、如何解读状态、修复建议与报告格式 |

## 使用方式

在 JiuwenSwarm 扩展中心启用本插件，对话输入区勾选插件 chip，然后发送推荐问法，例如：

- 「全面体检本机已安装的三方 Agent（codex、Claude Code、Kimi Code、Zcode 等），输出健康报告」
- 「只检测 codex 和 Claude Code 的安装与登录状态」
- 「检测所有 Agent 的命令行、版本与网络连通性，并给出修复建议」

## 安全说明

全程只读：不修改文件、不执行登录、不读取密钥内容。如需执行修复（安装、登录、改配置），助手会先征得你的确认。
