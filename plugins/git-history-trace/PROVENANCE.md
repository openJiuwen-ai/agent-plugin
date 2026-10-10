# PROVENANCE / 来源说明

- **本插件**：`git-history-trace`，Apache-2.0 许可（见本目录 [LICENSE](./LICENSE)），代码与文档均为本仓独立编写。
- **功能参考**：modelcontextprotocol/servers 仓库中的 git 参考实现（参考 MCP）。注意：该项目根许可声明存在 MIT 到 Apache-2.0 的过渡，各文件许可可能不一致；本插件**未复制、移植或改写其任何源码、提示词、测试或配置**，也不将其视为生产级背书。
- 本插件的实现完全基于 **Git 官方 CLI 文档**（log / blame / show / rev-parse / check-attr 的公开行为）独立编写：`--verify --end-of-options` 引用解析、`check-attr` 自定义 diff 驱动守卫、`--no-optional-locks --no-pager` 只读调用、porcelain 解析与边界声明，均为本仓的独立设计，与参考项目不存在代码层面的派生关系。
- 参考项目的许可不适用于本包；引用其能力时请遵循其自身许可。
