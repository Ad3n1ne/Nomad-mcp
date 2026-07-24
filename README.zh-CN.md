# nomad

[English README](README.md)

nomad 是一个运行在本地的 MCP Server，用来打通“本地写代码 + 远端验证运行”的 Agentic 远程开发工作流。

Codex 推荐使用按项目常驻的 Streamable HTTP daemon。stdio 模式继续保留，用于兼容旧客户端和一次性调用。

## 特性

- 一个本地项目可以配置多个远端 target。
- 通过项目内 `.nomad.json` 绑定远端机器、远端目录、同步规则、运行时和限制参数。
- 初始化时探测本地项目、SSH alias、本地代理、远端硬件和运行时。
- 使用 `rsync` 增量同步，支持 `.gitignore` 转换和 `--delete` dry-run 保护。
- 支持从远端拉取产物到本地项目目录。
- 支持短命令远端执行，并自动截断输出，避免撑爆智能体上下文。
- 支持通过远端 `tmux` 管理长任务。
- 可选持久反向 SSH 隧道，让远端命令和长任务复用本地代理。
- 内置路径守卫、危险命令拦截、敏感信息脱敏和网络诊断。

## 依赖

- Python 3.11+
- `ssh`
- `rsync`
- 使用长任务时，远端机器需要安装 `tmux`
- 远端 target 推荐使用 SSH key 免密登录

persistent daemon 生命周期管理目前支持 macOS、Linux 和其他 POSIX 系统，
不支持 Windows daemon。stdio transport 不经过 daemon 生命周期，但项目目前
也没有声明或完成 Windows 支持验证。

## 安装

使用 PyPI 最新版本时，可以用 `uvx` 直接运行：

```bash
uvx nomad-mcp
```

如果想直接使用指定 GitHub tag，不等 PyPI 同步：

```bash
uvx --from git+https://github.com/Ad3n1ne/Nomad-mcp.git@v0.2.0 nomad
```

或者用 `pipx` 安装成隔离的全局命令：

```bash
pipx install nomad-mcp
```

## MCP 客户端配置

### 推荐方式：常驻 HTTP daemon

在每个本地项目中启动一个 daemon：

```bash
nomad daemon start --project "$PWD"
nomad daemon status --project "$PWD"
```

`status` 会返回该项目专属的 `url` 和 `token_env_var`。推荐通过这个环境变量
配置 bearer token，而不是把 token 内联到客户端配置。`daemon token` 只向
stdout 写入 secret，便于 shell substitution；请把输出当作凭据，不要写入日志。

生成引用该环境变量的 Codex TOML 配置：

```bash
nomad client-config \
  --transport http \
  --project "$PWD" \
  --name nomad-myproject \
  --format toml
```

也可以用 Codex CLI 直接注册。先从 `status` 读取非敏感的连接信息：

```bash
NOMAD_PROJECT="$PWD"
NOMAD_STATUS="$(nomad daemon status --project "$NOMAD_PROJECT")"
NOMAD_URL="$(python -c 'import json,sys; print(json.load(sys.stdin)["url"])' <<<"$NOMAD_STATUS")"
NOMAD_TOKEN_ENV_VAR="$(python -c 'import json,sys; print(json.load(sys.stdin)["token_env_var"])' <<<"$NOMAD_STATUS")"
codex mcp add nomad-myproject \
  --url "$NOMAD_URL" \
  --bearer-token-env-var "$NOMAD_TOKEN_ENV_VAR"
```

使用 Codex CLI，或者从终端启动 Codex 时，在同一个 shell 中导出 token 后再启动：

```bash
export "$NOMAD_TOKEN_ENV_VAR=$(nomad daemon token --project "$NOMAD_PROJECT")"
codex
```

macOS 上使用 Codex Desktop 时，把变量写入当前 GUI 登录会话：

```bash
launchctl setenv "$NOMAD_TOKEN_ENV_VAR" "$(nomad daemon token --project "$NOMAD_PROJECT")"
```

然后完全退出 Codex Desktop 并重新打开，让新进程继承该变量。这个 `launchctl`
值属于当前登录会话，注销账号或重启 Mac 后可能需要重新设置。

每个项目都有持久化的高位端口、独立 token 环境变量和独立 daemon 状态。
首次启动时，Nomad 会从项目哈希对应的候选端口开始确定性扫描，避开正在监听
或已被其他项目保留的端口。多个项目应使用不同 MCP 名称，例如 `nomad-api`、
`nomad-dataset`，并分别注册各自 status 返回的 URL。

daemon 生命周期命令：

```bash
nomad daemon status --project "$PWD"
nomad daemon restart --project "$PWD"
nomad daemon stop --project "$PWD"
```

升级 nomad 后，需要重启所有正在运行的项目 daemon，让常驻进程加载新代码。

Nomad 0.2.0 的 HTTP 服务只允许监听 loopback 地址。`nomad serve`、
`nomad daemon start` 和 server API 即使配置了 bearer token，也会拒绝
non-loopback host。远程监听要等未来加入 TLS 传输支持后再开放。

### 兼容 stdio 模式

`client-config` 默认仍输出 stdio 配置，兼容不支持 Streamable HTTP 的客户端和
原有用法。

推荐的 PyPI 免安装配置：

```json
{
  "mcpServers": {
    "nomad": {
      "command": "uvx",
      "args": ["nomad-mcp"]
    }
  }
}
```

如果想直接使用 GitHub 最新 tag：

```json
{
  "mcpServers": {
    "nomad": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/Ad3n1ne/Nomad-mcp.git@v0.2.0",
        "nomad"
      ]
    }
  }
}
```

TOML 配置通常类似：

```toml
[mcp_servers.nomad]
command = "uvx"
args = ["nomad-mcp"]
startup_timeout_sec = 120
```

如果你用 `pipx` 安装了全局命令，也可以这样配置：

```json
{
  "mcpServers": {
    "nomad": {
      "command": "nomad",
      "args": []
    }
  }
}
```

也可以用 CLI 打印配置片段：

```bash
nomad client-config
nomad client-config --runner github
nomad client-config --runner nomad --format toml
nomad client-config --transport stdio --name nomad
```

## 快速开始

1. 按上面的流程启动并注册项目 HTTP daemon。
2. 在本地项目目录中打开 Codex。
3. 首次使用 Nomad 工具前先调用 `health`。
4. 调用 `init_discover` 发现本地项目、SSH alias 和代理环境。
5. 选择一个 SSH target 和远端工作目录。
6. 调用 `init_save_config` 保存 `.nomad.json`。
7. 使用 `sync_push` 把本地代码同步到远端。
8. 使用 `run_remote` 执行短命令。
9. 使用 `task_start` 启动长任务，再用 `task_status` 或 `task_list` 查看状态。
10. 使用 `sync_pull` 拉取远端产物。

## 示例 `.nomad.json`

```json
{
  "project_name": "my_project",
  "mode": "remote",
  "default_target": "devbox",
  "targets": {
    "devbox": {
      "description": "Primary remote development machine",
      "ssh_host": "devbox",
      "remote_path": "/data/my_project",
      "local_subpath": null,
      "auto_create_remote_path": true,
      "network": {
        "use_proxy_for_ssh": false,
        "jump_host": null,
        "reverse_tunnel": {
          "enabled": false,
          "proxy_scheme": "socks5"
        }
      },
      "sync": {
        "respect_gitignore": true,
        "extra_excludes": []
      },
      "runtime": {
        "interpreter": null,
        "extra_env": {}
      },
      "limits": {
        "command_timeout_seconds": 60,
        "max_output_lines": 200,
        "max_output_bytes": 10240
      }
    }
  }
}
```

`run_remote` 的超时由 `limits.command_timeout_seconds` 控制。下载、编译、训练、Fuzzing、批处理这类慢任务更适合用 `task_start` 放到远端 tmux 后台运行。

## Codex 调用约束

- 每个 Codex task 首次调用 Nomad 前，先调用 `health`。
- `run_remote` 只用于短同步探针和短命令。
- 上传、编译、训练、服务进程、扫描、批处理统一用 `task_start`。
- 有副作用的调用如果发生客户端超时，不要立刻重试；先检查远端状态或任务状态，
  避免同一操作执行两次。
- 从 stdio 迁移到常驻 HTTP daemon 后，Codex 的 stdio 子进程 transport 即使失效，
  也不会再连带终止 Nomad 服务和服务状态。
- HTTP 不能消除所有断连：Codex、本机网络栈或 daemon 自身仍可能重启。此时先让
  客户端重连并检查 `daemon status`，仅在 daemon 不健康时重启它。
- 继续使用旧 stdio 模式时，如果 Codex 外层报 `Transport closed`，停止在当前
  task 里反复重试并重启 MCP transport。清理 Codex 拉起后残留的 stdio 进程：

```bash
nomad doctor --kill-stale-mcp
```

## 工具列表

- `init_discover`：发现本地项目、SSH alias 和代理配置。
- `init_verify_and_probe`：验证 SSH 可达性并探测远端硬件/运行时。
- `init_save_config`：校验并保存 `.nomad.json`。
- `init_probe_target`：刷新 target 的硬件和运行时信息。
- `sync_push`：同步本地代码到远端工作目录。
- `sync_pull`：把远端文件或目录拉回本地 `remote_artifacts/`。
- `run_remote`：在远端工作目录执行短命令。
- `task_start`：启动远端 tmux 长任务。
- `task_status`：查看指定任务状态和日志尾部。
- `task_list`：列出当前项目的远端任务。
- `task_kill`：停止任务，但保留日志和状态文件。
- `net_diagnose`：执行只读 SSH/网络诊断。
- `tunnel_start`、`tunnel_status`、`tunnel_stop`：管理持久反向隧道。

## 安全说明

nomad 会通过 SSH 执行命令，也会使用 `rsync` 同步文件。请只在可信本地项目和可信远端机器上使用。

它内置了本地/远端路径校验、危险命令拦截、`.nomad.json` 同步排除、敏感信息脱敏、输出截断和 `rsync --delete` dry-run 保护。这些机制能降低风险，但不能把不可信智能体或不可信远端机器变成可信对象。

## 开发

```bash
python -m pip install -e .[dev]
nomad --version
nomad doctor
nomad doctor --kill-stale-mcp --dry-run
python -m pytest
python -m compileall -q src tests
```

## License

MIT
