# Docker Inspector / Docker 检查助手

> **一句话定位（zh）**：只读诊断本机 Docker——容器清单、健康状态 / 端口映射检查、脱敏后的最近日志，绝不做任何变更操作。
>
> **One-liner (en)**: Read-only local Docker diagnostics — container listing, health/status and port-mapping inspection, and best-effort redacted recent logs, with zero mutations.

## 核心能力

- **list_containers**：`docker ps -a --format {{json .}}`，返回容器 ID / 名称 / 镜像 / 状态 / 端口（limit 上限 200，truncated 标记）。
- **inspect_container**：`docker inspect <container>`，只返回白名单字段（State、Health、RestartCount、端口映射等）；**Env / Labels / Cmd / Entrypoint / Health.Log / Mounts 一律不返回**，不透出原始 inspect 对象。
- **logs**：`docker logs --tail N --timestamps <container>`，输出有界（20000 字符 / 行 2000 字符），按常见凭据模式（password/token/Bearer/AKIA/私钥块）**尽力脱敏**并明示 best-effort 局限。
- **安全设计**：无 shell、参数列表传参；容器名 / ID 严格校验（`^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`，不能是选项）；单次调用超时（默认 10s，上限 30s）；CLI 缺失或 daemon 不可达返回明确 `success=false`。
- **明确边界**：只执行 ps / inspect / logs 三个只读子命令；绝不 start / stop / restart / exec / build / pull / compose。

## 组件协作

```
用户请求（容器清单 / 状态排查 / 看日志）
   │
   ▼
skills/docker-diagnostics ── 何时查、如何解读健康状态、脱敏注意事项
   │
   ▼
tools/docker_inspector_tool.py ── DockerInspectorTool
   ├── list_containers ──► docker ps -a（JSON 行解析）
   ├── inspect_container ──► docker inspect（白名单字段）
   └── logs ──► docker logs --tail（脱敏 + 截断）
   （无 shell / 严格校验 / 超时与输出有界 / 只读子命令白名单）
```

本插件不包含 rail。前置依赖：本机已安装 docker CLI 并在 PATH 中（本插件不安装、不启动 Docker）。

## 目录结构

```
docker-inspector/
├── manifest.json
├── README.md
├── LICENSE                    # Apache-2.0（与仓库根一致）
├── PROVENANCE.md
├── skills/docker-diagnostics/SKILL.md
└── tools/docker_inspector_tool.py
```
