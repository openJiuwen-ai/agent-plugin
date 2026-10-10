# JiuwenSwarm Agent Plugin

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](./LICENSE)

JiuwenSwarm（聚问蜂群）**官方 plugin 插件包集合仓**：对应 Hub 市场资产类型
`agent-plugin`，为智能体提供**技能（skill）/ 工具（tool）/ 生命周期钩子（rail）**
三类能力扩展的一站式内容源。

- Hub 在线安装：扩展中心 → 搜索 / 安装本仓发布的插件
- 产品内置：旗舰插件随 jiuwenswarm `built_in` 目录分发
- 开发者直装：把 `plugins/<name>/` 拷贝到 `~/.jiuwenswarm/agent/workspace/plugins/plugin_packages/local/`

> English overview: [README_EN.md](./README_EN.md)

## 插件目录

<!-- catalog:begin -->
| 插件 | 版本 | 分类 | 能力组成 | 简介 |
| --- | --- | --- | --- | --- |
| [build-log-diagnosis](./plugins/build-log-diagnosis) | 0.1.0 | Engineering | 1 skill + 1 tool | 本地提取构建日志中的错误上下文、源码位置和可能原因，区分证据与推断并给出可执行验证步骤。 |
| [commit-draft](./plugins/commit-draft) | 0.1.0 | Engineering | 1 skill + 1 tool | 读取指定 Git 差异与仓库模板，生成有证据的提交信息和 PR 描述草稿，不自动提交或推送。 |
| [developer-docs](./plugins/developer-docs) | 0.1.0 | Engineering | 1 skill + 1 tool | 识别项目依赖声明和锁定版本，按指定库名与问题查询 Context7，返回文档证据并标明版本限制。 |
| [docker-inspector](./plugins/docker-inspector) | 0.1.0 | Engineering | 1 skill + 1 tool | 只读诊断本机 Docker：列出容器、检查健康状态与端口映射、查看脱敏后的最近日志，不做变更。 |
| [git-history-trace](./plugins/git-history-trace) | 0.1.0 | Engineering | 1 skill + 1 tool | 只读追踪本地 Git 文件的提交历史与行级 blame 归属，可展示指定提交证据，不改动工作区。 |
| [http-api-probe](./plugins/http-api-probe) | 0.1.0 | Engineering | 1 skill + 1 tool | 探测本机 HTTP 接口：GET/HEAD 状态、耗时与响应预览，支持状态码与 JSON 字段断言。 |
| [notebook-inspector](./plugins/notebook-inspector) | 0.1.0 | Engineering | 1 skill + 1 tool | 不执行任何单元格，静态读取本地 notebook：大纲、预览、报错与执行顺序提示，可导出清洗副本。 |
| [page-acceptance](./plugins/page-acceptance) | 0.1.0 | Engineering | 1 skill + 1 tool | 在独立浏览器会话中按明确步骤验收本地页面，记录断言结果、错误与截图，失败不会报告为通过。 |
| [pr-comment-assistant](./plugins/pr-comment-assistant) | 0.1.0 | Engineering | 1 skill + 1 tool | 读取 AtomGit 检视意见并关联代码位置，整理待办后按选定意见修改，生成带验证证据的回复草稿。 |
| [sqlite-inspector](./plugins/sqlite-inspector) | 0.1.0 | Engineering | 1 skill + 1 tool | 只读查看本地 SQLite 库：表清单、列与索引结构、受限查询与执行计划，拒绝一切写入。 |
<!-- catalog:end -->

> 上表由 `python scripts/catalog.py` 生成，请勿手改。

## 仓库结构

```
agent-plugin/
├── plugins/                  # 插件包实体（扁平一层，一包一目录；内容 = 用户拿到的内容）
├── templates/plugin-template/   # 脚手架模板（scripts/init.py 消费）
├── scripts/                  # 工具链：init / validate / catalog / build / publish / sync_builtin
├── catalog/catalog.json      # 生成物：插件目录清单（lockfile 式）
├── docs/                     # 插件包规范、发布流程、仓库设计文档
└── tests/                    # 工具链单测 + 各插件行为测试（不随包分发）
```

## 快速开始（开发一个插件）

```bash
# 1. 生成骨架（可选预置组件）
python scripts/init.py my-plugin --skill usage-guide --tool counter

# 2. 填充 manifest.json / README.md / 组件内容，消灭全部 [TODO]
#    规范见 docs/plugin-spec.md

# 3. 三层校验（L0 规范 / L1 静态 / L2 热加载，L2 需 jiuwenswarm 运行环境）
python scripts/validate.py --only my-plugin

# 4. 更新目录清单并提交 PR（PR 质量门禁见 .github/workflows/pr-quality.yml）
python scripts/catalog.py
```

完整贡献流程与安全审查要求见 [CONTRIBUTING.md](./CONTRIBUTING.md)。

## 文档

- [插件包规范（manifest / skill / tool / rail）](./docs/plugin-spec.md) — 权威来源
- [Hub 发布流程](./docs/hub-publish.md)
- [PR 安全审查清单](./docs/review-checklist.md)
- [仓库结构设计](./docs/design/repo-structure-design.md)

## 许可证

[Apache-2.0](./LICENSE) © Huawei Technologies Co., Ltd.
