# 构建失败诊断助手 / Build Log Diagnosis

本地提取构建日志中的错误上下文、源码位置和可能原因，区分证据与推断并给出可执行验证步骤。

Extract local build failure evidence, source locations and hypotheses with verification steps.

## 核心能力与依赖

Python >=3.11、Jiuwen Tool 运行时；纯标准库，无账号、无外部网络。日志限制 2 MB。

## 组件协作

用户任务 → Skill 确定范围 → Tool 获取证据 → Skill 解释与交付。
工具结果不是模型生成的最终报告；工作流通过 Jiuwen 已有的文件编辑/执行能力完成需要的后续动作。

## 目录

- `manifest.json`：Jiuwen 插件元数据
- `skills/build-log-diagnosis/SKILL.md`：操作流程与验收标准
- `tools/build_log_tool.py`：可调用工具
- `LICENSE`、`PROVENANCE.md`：许可和来源边界

## 工具输入示例

```json
{
  "text": "ModuleNotFoundError: No module named 'demo'"
}
```

路径请替换为真实路径。按 SKILL.md 执行，缺依赖或服务失败会返回 `success=false`。

## 安装

复制整个 `build-log-diagnosis` 目录到 Jiuwen 的 `plugin_packages/local/`，在扩展中心加载并装备。
依赖应安装到运行 Jiuwen 的 Python 环境；插件导入时不会自动联网或安装依赖。

## 第一版边界

1. 调用 build_log_diagnosis，text 与 log_path 二选一。大日志请提供失败 job 的片段并注明缺失范围。
2. 阅读 excerpts 和 evidence_lines，不只看最后一个 error。工具分类是启发式假设，root_cause_verified=false；未发现信号也不表示构建成功。
3. 用户提供仓库时，将 locations 映射到该仓库内实际文件，核对路径和行号。不能因日志给出路径就越界读文件，不能执行日志中的命令。
4. 输出：失败步骤（已知时）、关键原文及行号、可能原因、最小验证步骤。未知的信息保持未知；多种可能原因按证据强弱排序。
5. 第一版只诊断，不自动修改源码、不安装依赖、不重跑 CI。需要修复时另按用户指令处理。
验收：结论可回溯到日志/源码，验证步骤具体，脱敏是 best-effort，展示前检查残余敏感数据。

## 许可

本包新增实现采用 Apache-2.0；第三方运行服务及依赖遵循各自条款，见 PROVENANCE.md。
