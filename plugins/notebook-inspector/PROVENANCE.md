# PROVENANCE / 来源说明

- **本插件**：`notebook-inspector`，Apache-2.0 许可（见本目录 [LICENSE](./LICENSE)），代码与文档均为本仓独立编写。
- **功能参考**：datalayer/jupyter-mcp-server（BSD-3-Clause 许可项目）。本插件仅将其作为「notebook 检查 / 清洗类工具应具备哪些能力」的**功能性参考**；**未复制、移植或改写其任何源码、注释、测试或配置**。
- 本插件的 JSON 解析逻辑、预览截断策略、跳过图片 / base64 的处理方式、execution_count 顺序启发式、独占创建（拒绝覆盖）的安全导出与错误处理，均为独立实现，与参考项目不存在代码层面的派生关系。
- 参考项目的 BSD-3-Clause 许可不适用于本包；引用其能力时请遵循其自身许可。
