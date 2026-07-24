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

使用 `pipx` 安装后，在本地项目中启动 daemon：

```bash
nomad daemon start --project "$PWD"
nomad daemon status --project "$PWD"
```

生成该项目的 Codex 配置：

```bash
nomad client-config \
  --transport http \
  --project "$PWD" \
  --name nomad-myproject \
  --format toml
```

生成的配置只引用 bearer-token 环境变量，不保存 token。启动 Codex 前导出它：

```bash
export NOMAD_TOKEN_ENV_VAR="$(nomad daemon status --project "$PWD" |
  python -c 'import json,sys; print(json.load(sys.stdin)["token_env_var"])')"
export "$NOMAD_TOKEN_ENV_VAR=$(nomad daemon token --project "$PWD")"
codex
```

macOS Codex Desktop 使用 `launchctl setenv` 设置同一个变量，然后彻底退出并
重新打开 Codex：

```bash
launchctl setenv "$NOMAD_TOKEN_ENV_VAR" \
  "$(nomad daemon token --project "$PWD")"
```

直接使用 `codex mcp add`、多项目隔离、生命周期命令、升级、安全边界和故障
排查见 [Persistent MCP Daemon](docs/09-persistent-daemon.md)。

### stdio 兼容模式

不支持 Streamable HTTP 的客户端可以直接启动 nomad：

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

等价 TOML：

```toml
[mcp_servers.nomad]
command = "uvx"
args = ["--from", "nomad-mcp", "nomad"]
startup_timeout_sec = 120
```

`nomad client-config` 可以为两种 transport 生成 JSON 或 TOML。

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
