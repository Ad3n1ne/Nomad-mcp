# nomad

[English README](README.md)

nomad 是一个运行在本地的 MCP Server，用来打通“本地写代码 + 远端验证运行”的 Agentic 远程开发工作流。

它不是 Codex 专属工具。只要你的智能体开发环境支持通过 command + args 启动 stdio MCP server，就可以接入 nomad。

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

## 安装

推荐用 `uvx` 直接运行：

```bash
uvx nomad-mcp
```

或者用 `pipx` 安装成隔离的全局命令：

```bash
pipx install nomad-mcp
```

## MCP 客户端配置

不同 MCP 客户端的配置文件位置不同。

推荐的免安装配置：

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
nomad client-config --runner nomad --format toml
```

## 快速开始

1. 在本地项目目录中打开支持 MCP 的智能体开发环境。
2. 调用 `init_discover` 发现本地项目、SSH alias 和代理环境。
3. 选择一个 SSH target 和远端工作目录。
4. 调用 `init_save_config` 保存 `.nomad.json`。
5. 使用 `sync_push` 把本地代码同步到远端。
6. 使用 `run_remote` 执行短命令。
7. 使用 `task_start` 启动长任务，再用 `task_status` 或 `task_list` 查看状态。
8. 使用 `sync_pull` 拉取远端产物。

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
python -m pytest
python -m compileall -q src tests
```

## License

MIT
