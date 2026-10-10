# PROVENANCE / 来源说明

- **本插件**：`sqlite-inspector`，Apache-2.0 许可（见本目录 [LICENSE](./LICENSE)），代码与文档均为本仓独立编写。
- **功能参考**：0xOmarA/mcp-server-sqlite（社区 MCP 项目；上游宣称 MIT / Apache-2.0 可选许可，未拷贝其任何源码）。本插件仅将其作为「SQLite 检查类工具应具备哪些能力」的**功能性参考**；**未复制、移植或改写其任何源码、提示词、测试或配置**。
- 本插件的只读防护设计（URI mode=ro + PRAGMA query_only + sqlite3 authorizer + 进度处理器资源上限）、多语句检测、截断与错误处理均为基于 Python 标准库 `sqlite3` 公开文档的独立实现，与参考项目不存在代码层面的派生关系。
- 参考项目的许可不适用于本包；引用其能力时请遵循其自身许可。
