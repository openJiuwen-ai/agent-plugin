---
name: docker-diagnostics
description: 本机 Docker 容器的只读诊断。当用户想查看容器清单、某个容器的健康状态 / 重启情况 / 端口映射，或排查容器日志（需脱敏）时使用；配合 docker_inspector 工具，绝不做任何变更操作。
---

# Docker 诊断（docker-diagnostics）

## 目标

只读回答三类问题：本机在跑什么容器（清单）、某个容器现在什么状态（健康 / 重启 / 端口）、它最近输出了什么（脱敏日志）。绝不执行 start/stop/restart/exec/build/pull 等变更。

## 工作流

1. `docker_inspector` action=list_containers 拿到容器清单（含已停止），定位目标容器名 / ID。
2. action=inspect_container 查看该容器：State（status/running/exit_code/restart_count）与 Health（healthy/unhealthy + failing_streak）；注意结果刻意排除 Env / Labels / 命令行等敏感字段，属于设计行为。
3. 需要日志时 action=logs（tail 控制行数）：先看 redaction 说明——脱敏是尽力而为，引用日志时避免原样转发敏感行。
4. CLI 缺失或 daemon 不可达会得到明确 success=false：向用户转述原因（未安装 / 未运行 / 无权限），不要尝试代替用户安装或启动 Docker。

## 验收标准

- 全程只读：未执行任何变更类子命令（可用 `docker events` 或用户复核确认）。
- 输出有界：count/truncated 明确；inspect 只含白名单字段。
- 诊断结论区分「证据」（state/health/日志行）与「推断」（如 OOM、镜像问题猜测）。
