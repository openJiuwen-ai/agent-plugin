# 页面验收助手 / Page Acceptance

在独立浏览器会话中按明确步骤验收本地页面，记录断言结果、错误与截图，失败不会报告为通过。

Check a local page with explicit browser steps, assertions and screenshot evidence in an isolated session.

## 核心能力与依赖

Python >=3.11、Jiuwen Tool 运行时、playwright>=1.48,<2，以及 Chromium 浏览器；安装后执行 python -m playwright install chromium。

## 组件协作

用户任务 → Skill 确定范围 → Tool 获取证据 → Skill 解释与交付。
工具结果不是模型生成的最终报告；工作流通过 Jiuwen 已有的文件编辑/执行能力完成需要的后续动作。

## 目录

- `manifest.json`：Jiuwen 插件元数据
- `skills/page-acceptance/SKILL.md`：操作流程与验收标准
- `tools/page_acceptance_tool.py`：可调用工具
- `LICENSE`、`PROVENANCE.md`：许可和来源边界

## 工具输入示例

```json
{
  "url": "http://127.0.0.1:8000",
  "output_dir": "/path/to/evidence",
  "steps": [
    {
      "action": "assert_visible",
      "selector": "body"
    }
  ]
}
```

路径请替换为真实路径。按 SKILL.md 执行，缺依赖或服务失败会返回 `success=false`。

## 安装

复制整个 `page-acceptance` 目录到 Jiuwen 的 `plugin_packages/local/`，在扩展中心加载并装备。
依赖应安装到运行 Jiuwen 的 Python 环境；插件导入时不会自动联网或安装依赖。

## 第一版边界

1. 明确本地 URL、验收步骤和实际期望结果；从页面/源码确定选择器，禁止猜测。第一版只访问 localhost/127.0.0.1/[::1]，其他 HTTP 请求被阻止，WebSocket 被阻止。
2. 将步骤转成 click/fill/assert_visible/assert_text；至少一个断言，最多 20 步。只有 assert_visible(body) 不足以证明业务功能，必须断言用户真正关心的结果。
3. output_dir 使用用户指定目录或宿主提供的会话输出目录。截图可能包含业务数据；使用测试账号/测试数据。新浏览器不复用用户登录态，操作会产生应用侧效果，执行范围以用户给定验收任务为准。
4. 调用 page_acceptance。缺浏览器时给出安装说明，不假造截图。遇到失败停止后续步骤，检查 report.json、失败截图和 browser_errors。
5. 输出已执行步骤、实际通过/失败、未执行步骤和截图路径。控制台错误为辅助证据，不能单独证明功能通过；失败也不自动修改页面代码。
验收：每项结论对应真实断言和产物，说明被阻止的外部资源可能影响页面。

## 许可

本包新增实现采用 Apache-2.0；第三方运行服务及依赖遵循各自条款，见 PROVENANCE.md。
