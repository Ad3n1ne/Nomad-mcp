# `.nomad.json` — Project ID Schema

Each project root directory contains one of these files. Whenever any MCP Server tool is invoked, **the first step must be to read this file**, serving as the context boundary for that operation.

No file = uninitialized = all remote operations are rejected.

> **Language-agnostic**: This MCP Server is itself implemented in Python, but the projects it manages can be in any language (Go, Rust, C, Python, etc.). Runtime detection within `hardware` is automatic; pure C/Go projects do not need to fill in `interpreter` under `runtime`.

---

## Design Note: Why `targets` instead of a single `network`

A single local workspace often maps to multiple remote servers, each with completely independent requirements:

- Push `data_pipeline/` to the data processing machine
- Push `model/` to the GPU training machine
- Push the entire project to the integration test machine

Each machine has its own SSH host, remote path, network configuration, runtime environment, and sync filter rules.

Therefore the Schema adopts a **`targets` named-target collection** design, rather than a single `network` field.

---

## Full Schema

```json
{
  "version": "1",

  "project_name": "string — unique project identifier, used as the tmux session prefix; only letters/digits/hyphens/underscores allowed",

  "mode": "remote | local",

  "default_target": "string | null — the default target name used when no target is specified. When null and targets has exactly one key, it automatically falls back to that unique key; otherwise it is required and must exist in targets",

  "targets": {
    "<target_name>": {

      "description": "string — purpose description for this machine, filled in by the user during initialization. The AI uses this field to autonomously decide which machine to use and which code to push in multi-target scenarios. For example: 'GPU training machine, runs model training', 'Data processing machine, runs the pipeline', 'Integration test machine'",

      "ssh_host": "string — Host alias in ~/.ssh/config, or user@ip format. Defaults to passwordless (key) authentication; no password field",

      "remote_path": "string — absolute path of the remote working directory, e.g. /root/workspace/project",

      "local_subpath": "string | null — only sync a subdirectory of the local project (relative path); null means sync the entire project root",

      "auto_create_remote_path": "bool — whether to auto mkdir -p if the remote directory does not exist, default true",

      "hardware": {
        "os": "string — operating system, e.g. Linux 6.1.0 x86_64. Auto-detected during initialization, read-only",
        "cpu_cores": "int — number of logical CPU cores, the nproc result. Auto-detected during initialization",
        "memory_total": "string — total memory, e.g. '128Gi'. Auto-detected during initialization",
        "disk_available": "string — available space on the partition where remote_path resides, e.g. '500G'. Auto-detected during initialization",
        "gpu": [
          {
            "name": "string — GPU model, e.g. 'NVIDIA A100 80GB PCIe'",
            "memory_total": "string — GPU memory, e.g. '80GiB'"
          }
        ],
        "detected_runtimes": [
          {
            "lang": "string — language/runtime type, e.g. 'python', 'node', 'go', 'ruby'",
            "type": "string — source type, e.g. 'system', 'conda', 'venv', 'nvm'",
            "name": "string — environment name (conda env name, venv directory name, etc.); for system it is always 'system'",
            "bin": "string — absolute path of the executable, e.g. '/root/.venv/bin/python'",
            "version": "string — version number, e.g. '3.11.4', '20.11.0'"
          }
        ],
        "probed_at": "string — ISO8601 timestamp recording the last probe time"
      },

      "network": {
        "use_proxy_for_ssh": "bool — whether the SSH connection needs to go through the local proxy, default false",
        "jump_host": "string | null — bastion/jump host alias; when present, SSH uses the -J flag, default null",
        "reverse_tunnel": {
          "enabled": "bool — whether to enable the reverse tunnel to share the local proxy with the remote, default false",
          "proxy_scheme": "socks5 | http — proxy protocol injected when the remote uses the tunnel, default socks5",
          "local_proxy_port": "int — local proxy listening port, e.g. 7890",
          "remote_bind_port": "int — remote bind port, defaults to the same value as local_proxy_port"
        }
      },

      "sync": {
        "respect_gitignore": "bool — automatically convert .gitignore into rsync --exclude rules, default true",
        "extra_excludes": ["array of string — extra exclude rules, e.g. '*.log', 'tmp/'"]
      },

      "runtime": {
        "interpreter": "string | null — main interpreter/runtime path. During initialization the AI guides the user to choose from hardware.detected_runtimes and fills this in. Python projects fill in the python path; Node.js fills in the node path; compiled languages like Go/Rust are usually null (run the build artifact directly)",
        "extra_env": {
          "KEY": "VALUE — extra environment variables injected when executing remote commands"
        }
      },

      "limits": {
        "command_timeout_seconds": "int — remote command timeout, default 60",
        "max_output_lines": "int — output truncation line count, default 200",
        "max_output_bytes": "int — output truncation byte count, default 10240"
      }

    }
  }
}
```

---

## `hardware` Field Notes

### Auto-detected, not manually filled

The `hardware` field is probed and filled in automatically by `init_save_config` before writing the config; users **do not need to fill it in manually**.

Probe commands (executed in a single SSH invocation):

```bash
ssh -o ConnectTimeout=5 -o BatchMode=yes {host} "
  uname -srom;
  nproc;
  free -h | grep Mem | awk '{print \$2}';
  df -h {remote_path} | tail -1 | awk '{print \$4}';
  nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo '__no_gpu__';
  python3 --version 2>/dev/null || echo '__no_python__';
  which python3 2>/dev/null || echo '__not_found__'
"
```

### How the AI Uses Hardware Information

The `hardware` field is exposed to the AI via MCP Resources, so the AI knows each machine's capabilities at the start of a conversation:

| Scenario | AI Behavior |
|---|---|
| Has GPU | Automatically suggests adding `CUDA_VISIBLE_DEVICES`, recommends a suitable batch size |
| Low memory | Alerts the user about OOM, suggests reducing batch size or enabling gradient checkpointing |
| Insufficient disk space | Reminds the user to check space before syncing |
| Python version too old | Warns that some libraries may be incompatible |

### Refreshing Hardware Information

Hardware information is a snapshot and is not auto-updated. If the machine's configuration changes (GPU swap, memory expansion), use the `init_probe_target` tool to re-probe and update the `hardware` field of the corresponding target.

---

## SSH Authentication Notes

**The default assumption is that all targets have passwordless (SSH key) authentication configured**; there is no password field in the schema.

During the initialization flow, once `ssh_host` is obtained, connectivity is verified immediately:

```bash
ssh -o ConnectTimeout=5 -o BatchMode=yes {host} "echo ok"
```

| Result | Handling |
|---|---|
| Returns `ok` | Connectivity confirmed, continue probing hardware info |
| `Connection timed out` | Error: target unreachable, suggest checking IP / firewall / TUN proxy routes |
| `Permission denied (publickey)` | Error: key not configured, suggest running `ssh-copy-id {host}` |
| `Host key verification failed` | Error: suggest running `ssh-keyscan {ip} >> ~/.ssh/known_hosts` |

If the connection fails, **initialization aborts** and `.nomad.json` is not written.

---

## Full Examples

### Scenario 1: One project, three servers

```json
{
  "version": "1",
  "project_name": "ml-system",
  "mode": "remote",
  "default_target": "gpu",

  "targets": {
    "gpu": {
      "ssh_host": "aliyun-gpu",
      "remote_path": "/root/workspace/ml-system",
      "local_subpath": null,
      "auto_create_remote_path": true,
      "hardware": {
        "os": "Linux 5.15.0 x86_64 GNU/Linux",
        "cpu_cores": 32,
        "memory_total": "128Gi",
        "disk_available": "800G",
        "gpu": [
          {"name": "NVIDIA A100 80GB PCIe", "memory_total": "80GiB"}
        ],
        "detected_runtimes": [
          {"lang": "python", "type": "system", "name": "system", "bin": "/usr/bin/python3", "version": "3.11.4"},
          {"lang": "python", "type": "conda", "name": "ml-env", "bin": "/root/miniconda3/envs/ml-env/bin/python", "version": "3.11.2"}
        ],
        "probed_at": "2026-06-30T20:00:00+08:00"
      },
      "network": {
        "use_proxy_for_ssh": false,
        "jump_host": null,
        "reverse_tunnel": {
          "enabled": true,
          "proxy_scheme": "socks5",
          "local_proxy_port": 7890,
          "remote_bind_port": 7890
        }
      },
      "sync": {
        "respect_gitignore": true,
        "extra_excludes": ["data/raw/", "checkpoints/", "*.safetensors"]
      },
      "runtime": {
        "interpreter": "/root/miniconda3/envs/ml-env/bin/python",
        "extra_env": {
          "CUDA_VISIBLE_DEVICES": "0",
          "TOKENIZERS_PARALLELISM": "false"
        }
      },
      "limits": {
        "command_timeout_seconds": 300,
        "max_output_lines": 200,
        "max_output_bytes": 10240
      }
    },

    "data-server": {
      "ssh_host": "internal-data",
      "remote_path": "/data/workspace/ml-system/pipeline",
      "local_subpath": "data_pipeline/",
      "auto_create_remote_path": true,
      "hardware": {
        "os": "Linux 5.15.0 x86_64 GNU/Linux",
        "cpu_cores": 64,
        "memory_total": "256Gi",
        "disk_available": "10T",
        "gpu": [],
        "detected_runtimes": [
          {"lang": "python", "type": "system", "name": "system", "bin": "/usr/bin/python3", "version": "3.10.12"}
        ],
        "probed_at": "2026-06-30T20:00:00+08:00"
      },
      "network": {
        "use_proxy_for_ssh": false,
        "jump_host": "bastion",
        "reverse_tunnel": {"enabled": false}
      },
      "sync": {
        "respect_gitignore": true,
        "extra_excludes": ["*.log", "cache/"]
      },
      "runtime": {
        "interpreter": null,
        "extra_env": {"DATA_ROOT": "/data/datasets"}
      },
      "limits": {
        "command_timeout_seconds": 120,
        "max_output_lines": 200,
        "max_output_bytes": 10240
      }
    }
  }
}
```

### Scenario 2: Pure local project

```json
{
  "version": "1",
  "project_name": "local-web-app",
  "mode": "local"
}
```

---

## Field Constraints

| Field | Constraint |
|---|---|
| `project_name` | Only `[a-zA-Z0-9-_]` allowed, length ≤ 50 |
| `default_target` | Optional (if targets has only one key); otherwise required and must be an existing key in targets |
| `targets.<name>` | Key only allows `[a-zA-Z0-9-_]`, length ≤ 30 |
| `remote_path` | Must be an absolute path and must start with a whitelisted prefix (see `04-security.md`) |
| `local_subpath` | If not null, must be a relative path; `../` path traversal is not allowed |
| `hardware` | Read-only, auto-filled by tools; manual edits are not validated |
| `version` | Currently `"1"`, reserved for future Schema migrations |

## Default Values

The initialization tool writes as complete a config as possible; if the user manually edits and omits optional fields, defaults are normalized on load per the implementation spec:

| Field | Default |
|---|---|
| `local_subpath` | `null` |
| `auto_create_remote_path` | `true` |
| `network.use_proxy_for_ssh` | `false` |
| `network.jump_host` | `null` |
| `network.reverse_tunnel.enabled` | `false` |
| `network.reverse_tunnel.proxy_scheme` | `"socks5"` |
| `sync.respect_gitignore` | `true` |
| `sync.extra_excludes` | `[]` |
| `runtime.interpreter` | `null` |
| `runtime.extra_env` | `{}` |
| `limits.command_timeout_seconds` | `60` |
| `limits.max_output_lines` | `200` |
| `limits.max_output_bytes` | `10240` |

---

## Notes

- `.nomad.json` is recommended to be added to `.gitignore` (it contains ssh_host, tokens in extra_env, etc. that should not be committed to the repository)
- `runtime.interpreter` specifies the concrete interpreter used by the runtime (e.g. the python path inside a virtual environment); leave it empty to use the remote shell's default environment.
- Future extension: sensitive tokens in `extra_env` may optionally be stored in the system Keychain, with only a reference key kept in the config.
