# nomad

[English README](README.md)

nomad 是一个本地 MCP Server，用于“本地写代码、远端运行验证”的 Agentic
开发流程。它通过 `rsync` 同步代码、SSH 执行命令、远端 `tmux` 管理长任务，
并将产物拉回本地项目。

Codex 推荐使用按项目常驻的 Streamable HTTP daemon。stdio 模式继续用于兼容
旧客户端和一次性调用。

## 特性

- 一个本地项目可配置多个远端 target。
- 使用项目内 `.nomad.json` 保存配置。
- SSH 预检和只读网络诊断。
- 增量 `rsync` 推送和受保护的产物拉取。
- 短命令执行与远端 `tmux` 长任务管理。
- 可选持久反向 SSH 隧道。
- 路径守卫、危险命令拦截、输出限制和敏感信息脱敏。

## 依赖

- Python 3.11+、`ssh` 和 `rsync`
- 使用 SSH key 登录远端 target
- 使用长任务时远端需要安装 `tmux`

daemon 生命周期支持 macOS、Linux 和其他 POSIX 系统。目前未支持或验证
Windows。

## 安装

直接运行 PyPI 最新版本：

```bash
uvx --from nomad-mcp nomad
```

或安装隔离的全局命令：

```bash
pipx install nomad-mcp
```

## Codex 配置

`nomad codex setup` 只是可选的 Codex 配置适配器，不是协议要求。任何 MCP
宿主都可以直接使用标准 stdio 或 Streamable HTTP 连接 Nomad。

在希望 Codex 控制的项目中运行：

```bash
nomad codex setup --project "$PWD"
nomad codex doctor --project "$PWD"
```

适配器会启动或修复项目 daemon，并且只写入可信项目的
`.codex/config.toml`；它不会修改用户级 Codex 配置或项目信任状态。解决命令
报告的全局冲突后，彻底重启 Codex 以加载项目 MCP。

其他 MCP 宿主可以直接通过 stdio 启动 Nomad：

```json
{
  "mcpServers": {
    "nomad": {
      "command": "uvx",
      "args": ["--from", "nomad-mcp", "nomad"]
    }
  }
}
```

`nomad client-config` 可以为标准 stdio 或 Streamable HTTP 客户端生成 JSON
或 TOML。手工注册、生命周期、安全边界和排障见
[Persistent MCP Daemon](docs/09-persistent-daemon.md)。

## 快速开始

1. 启动并注册项目 daemon。
2. 在本地项目目录中打开 Codex。
3. 调用 `health`，然后调用 `init_discover`。
4. 选择 SSH target 和远端工作目录。
5. 使用 `init_save_config` 保存 `.nomad.json`。
6. 使用 `sync_push` 推送代码。
7. 使用 `run_remote` 执行短命令。
8. 使用 `task_start` 和 `task_status` 管理长任务。
9. 使用 `sync_pull` 拉取产物。

`run_remote` 只适合短同步操作。下载、编译、训练、服务和批处理应使用
`task_start`。有副作用的调用超时后，先检查状态再决定是否重试。

## 文档

- [项目概览](docs/00-overview.md)
- [`.nomad.json` schema 与示例](docs/01-schema.md)
- [工具和工作流](docs/02-tools.md)
- [网络与反向隧道](docs/03-network.md)
- [安全模型](docs/04-security.md)
- [上下文和输出限制](docs/05-context-defense.md)
- [工作区隔离](docs/06-workspace-isolation.md)
- [常驻 MCP daemon](docs/09-persistent-daemon.md)

## 安全

nomad 会通过 SSH 执行命令并使用 `rsync` 同步文件。请只在可信本地项目和
可信远端机器上使用。安全守卫可以降低风险，但不能让不可信智能体或主机变得
可信。

## 开发

```bash
python -m pip install -e .[dev]
nomad doctor
python -m pytest
python -m compileall -q src tests
```

## License

MIT
