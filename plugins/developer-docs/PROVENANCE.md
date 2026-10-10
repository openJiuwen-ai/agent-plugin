# 来源与许可记录

本包是为 Jiuwen 编写的新实现，采用 Apache-2.0。未复制 Codex 私有连接器、品牌素材或上游 Skill/脚本正文；未随包分发第三方实现。
功能参考不代表上游背书，不能标为 OpenAI 官方产品。发布前对实际分发依赖重新核查许可。

参考资料（核对日期：2026-09-29）：
- https://github.com/openai/skills/tree/main/skills/.curated/gh-address-comments （Apache-2.0；旧仓库已弃用，仅作为历史流程参考）
- https://github.com/openai/skills/tree/main/skills/.curated/gh-fix-ci （Apache-2.0；仅流程参考）
- https://github.com/openai/skills/tree/main/skills/.curated/playwright （Apache-2.0；仅功能参考）
- https://github.com/upstash/context7 （MIT；本包不分发其代码）
- https://context7.com/docs/api-reference/search/search-context7-documentation （公开接口契约）
- https://docs.atomgit.com/docs/apis/get-api-v-5-repos-owner-repo-pulls-number-comments/ （公开接口契约）

运行依赖：httpx（BSD-3-Clause，按需安装）；Playwright（Apache-2.0，仅页面验收需要，其浏览器及传递依赖另有声明）。不将可选运行依赖视为本包原创代码。
Context7 API、文档片段和 AtomGit 服务不由本包许可证授权；使用者配置自己的账号，遵守服务条款、数据权限和组织政策。查询时只发送明确指定的信息，不扫描或上传整个仓库。
