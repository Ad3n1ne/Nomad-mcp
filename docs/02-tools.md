# Tools — Full Design

---

## Tool Naming Convention

| Prefix | Meaning |
|---|---|
| `init_` | Initialization lifecycle |
| `run_` | Command execution |
| `sync_` | Code sync |
| `task_` | Long tasks / tmux management |
| `tunnel_` | Persistent reverse tunnel |
| `net_` | Network diagnostics |

## The `target` Parameter

All tools involving remote operations accept a `target: str = "default"` parameter.

- `"default"` → resolved to `default_target` in `.nomad.json`
- A specific name (e.g. `"gpu"`, `"data-server"`) → uses the corresponding target's configuration
- If the target does not exist, returns the list of available targets and performs no operation

At the start of a conversation, the AI should read the current project configuration first to learn the available targets, and proactively ask the user which one to use when necessary.

---

## I. Initialization Module

### `init_discover`

**When triggered**: The user says "help me initialize this project", or the AI detects there is no `.nomad.json` in the current directory and calls it automatically.

**Behavior**:
1. Read `os.getcwd()` to get the current project path and name
2. Read `~/.ssh/config`, extract all non-wildcard Host aliases
3. Detect the current directory: presence of `.gitignore`, `requirements.txt`, `package.json`, `go.mod`, `Makefile` (to identify project type)
4. Detect local proxy environment variables (`ALL_PROXY`, `HTTP_PROXY`, `HTTPS_PROXY`)

**Returns to the AI**:

```json
{
  "current_local_path": "/Users/me/code/vuln-fuzzer",
  "project_name": "vuln-fuzzer",
  "detected_type": "python",
  "available_ssh_hosts": ["aliyun-gpu", "lab-server"],
  "local_proxy_detected": true,
  "proxy_port": 7890,
  "has_gitignore": true,
  "config_exists": false
}
```

**Description (AI-facing)**:
> [Initialization only] Explore the local development environment context. Call when the user requests project initialization, or when the current directory is detected to have no .nomad.json. Returns current project info and the list of available SSH hosts, letting the AI guide the user through the choices.


### `init_verify_and_probe`

**When triggered**: After the user confirms a target's `ssh_host`, called before writing the config to verify connectivity and probe hardware.

**Parameters**: `ssh_host: str`, `remote_path: str`, `jump_host: str | None = None`

**Also needs user confirmation during initialization**:
- `target_name`: what to call this target (e.g. `gpu`, `data-server`)
- `description`: what this machine is for (e.g. "GPU training machine, runs model training") — the AI later uses this field to make autonomous decisions, no need to ask each time
- `local_subpath`: push only a subdirectory of the local project, or the whole project (optional, defaults to the whole project)

**Behavior**:

```
1. SSH connectivity verification:
   ssh -o ConnectTimeout=5 -o BatchMode=yes [-J {jump_host}] {ssh_host} "echo ok"
   ├── Timeout                        → Error: target unreachable, suggest checking IP/firewall/TUN proxy routes
   ├── Permission denied (publickey)  → Error: key not configured, suggest running ssh-copy-id {ssh_host}
   ├── Host key verification failed   → Error: suggest running ssh-keyscan {ip} >> ~/.ssh/known_hosts
   └── Returns "ok"                   → Continue

2. Hardware + environment probe (single SSH):
   uname -srom
   nproc
   free -h | grep Mem | awk '{print $2}'
   df -h {remote_path} 2>/dev/null | tail -1 | awk '{print $4}' || echo 'path_not_exist'
   nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo '__no_gpu__'

   # System Python
   python3 --version 2>/dev/null || echo '__no_python__'
   which python3 2>/dev/null || echo '__not_found__'

   # conda environments (if conda is present)
   conda env list 2>/dev/null | grep -v '^#' || echo '__no_conda__'

   # venv (look for pyvenv.cfg under the project directory and home)
   find {remote_path} ~ -maxdepth 4 -name "pyvenv.cfg" 2>/dev/null | head -10

   # Node.js (system + nvm)
   node --version 2>/dev/null || echo '__no_node__'
   which node 2>/dev/null || echo '__not_found__'
   ls ~/.nvm/versions/node 2>/dev/null || echo '__no_nvm__'

   # Go
   go version 2>/dev/null || echo '__no_go__'
   which go 2>/dev/null || echo '__not_found__'

   # Ruby
   ruby --version 2>/dev/null || echo '__no_ruby__'

3. Parse results, assemble the hardware object:
   - detected_runtimes list: covers python (system/conda/venv), node (system/nvm), go, ruby, and every other runtime found
   - Attach the probed_at timestamp
```

**Returns**: the `hardware` object (containing the `detected_runtimes` list), or a detailed error message on connection failure.

**If the connection fails: initialization aborts, does not proceed to the next step.**

**Runtime selection interaction** (AI side):

The AI prioritizes runtimes matching the project type (detected `requirements.txt` → Python, `package.json` → Node.js, `go.mod` → Go), with other languages collapsed:

```
I detected the following runtime environments:

  [Python]
  1. system   /usr/bin/python3                                  (3.11.4)
  2. conda: base   /root/miniconda3/bin/python                  (3.10.12)
  3. conda: ml-env /root/miniconda3/envs/ml-env/bin/python      (3.11.2)
  4. venv: .venv   /root/workspace/project/.venv/bin/python     (3.11.4)

  [Node.js]
  5. system   /usr/bin/node                                      (20.11.0)
  6. nvm: 18.19.0  ~/.nvm/versions/node/v18.19.0/bin/node

  [Go]
  7. system   /usr/local/go/bin/go                               (1.22.0)

Please choose which runtime to use for this target? (number, path, or skip)
```

After the user selects, write it into `runtime.interpreter`; if skipped, set it to `null` (applies to compiled languages like Go/Rust that run the artifact directly).

**Description (AI-facing)**:
> [Initialization only] Verify connectivity to the specified SSH host, and automatically probe remote hardware info (OS, CPU, memory, disk, GPU) and all available runtimes (Python/Node.js/Go/Ruby, etc.). Must be called after the user confirms ssh_host and before writing the config. Aborts on connection failure, returning the specific reason and suggestions. After probing, the AI should prioritize runtimes matching the project type, guide the user to select one, and write it into runtime.interpreter.


---

### `init_save_config`

**When triggered**: All targets have been verified via `init_verify_and_probe`, and the AI has assembled the complete config.

**Parameters**: Accepts a complete JSON string conforming to the Schema (including each target's `hardware` field, filled in by `init_verify_and_probe`).

**Behavior**:
1. Parse and validate the JSON (project_name format, remote_path whitelist, required fields, default_target existence)
2. If a target's `auto_create_remote_path=true`, execute `ssh {host} "mkdir -p {remote_path}"`
3. Write `{cwd}/.nomad.json`

**Returns**: The path written on success, or detailed validation error messages.

**Description (AI-facing)**:
> [Initialization only] Call after all targets are verified and all parameters are confirmed, to write the complete config into .nomad.json. The hardware field should already be filled in automatically by init_verify_and_probe; the user does not need to fill it manually.

---

### `init_probe_target`

**When triggered**: When the machine's hardware changes (GPU swap, memory expansion, system reinstall), refresh the corresponding target's `hardware` info.

**Parameters**: `target: str = "default"`

**Behavior**:
1. Read the current `.nomad.json`, get the target's `ssh_host` and `remote_path`
2. Execute the same probe commands as `init_verify_and_probe`
3. Update the `hardware` field and `probed_at` timestamp of that target in `.nomad.json`

**Description (AI-facing)**:
> Re-probe the specified target's hardware info and update the hardware field in the config file. Use after the machine's configuration changes.

---



## II. Command Execution Module

This is the heart of the entire MCP. The core is `run_remote`: `ssh host "command"`.

> **About local commands**: Operations like `git`, local file reads/writes, and local builds are already handled by the AI client (Cursor / Claude Code) itself via its local command execution capability; they do not need to be wrapped through the MCP. This module is **remote-only**.

---

### `run_remote`

**Applicable scenarios**: Compiling, running tests, viewing remote logs, installing dependencies, checking GPU status.

**Parameters**: `cmd: str`, `target: str = "default"`

**Implementation notes**:

1. Resolve the target, get its `ssh_host`, `remote_path`, `network`, `runtime`
2. If `jump_host` is configured:
   ```bash
   ssh -J {jump_host} -o ConnectTimeout=5 -o BatchMode=yes {ssh_host} \
     "cd {remote_path} && {env_exports} {cmd}"
   ```
   Otherwise:
   ```bash
   ssh -o ConnectTimeout=5 -o BatchMode=yes {ssh_host} \
     "cd {remote_path} && {env_exports} {cmd}"
   ```
3. Inject the environment variables from `runtime.extra_env` (convert to an `export KEY=VALUE &&` prefix)
4. **Interactive command detection**: If the command contains `vim`, `nano`, `top`, `htop`, `less`, `more`, `man`, reject it outright and suggest non-interactive alternatives (e.g. `cat`, `ps aux`, `head/tail`)
5. If SSH connection fails (ConnectTimeout), immediately return a diagnostic hint, don't hang waiting
6. Output truncation: see `05-context-defense.md`
7. Security blacklist pre-check: see `04-security.md`

**Description (AI-facing)**:
> Execute a command in the project directory on the specified target's remote server. With multiple targets, decide which machine to use based on each target's `description` field autonomously; do not interrupt the user for confirmation each time. Do not run interactive commands (vim/top/less, etc.); use non-interactive alternatives instead. If a command may take more than 60 seconds, switch to task_start to run it asynchronously inside tmux.

---

## III. Code Sync Module

### `sync_push` (local → remote)

**Parameters**: `target: str = "default"`

**When triggered**:
- The user explicitly says "sync" / "push" / "deploy"
- The AI **autonomously decides** whether a sync is needed before executing remote commands or starting long tasks — no need to ask the user, just push
- rsync does incremental sync by default; the destination is fixed within the `remote_path` whitelist, the risk is low, and the AI can trigger it proactively with confidence

**Push decision with multiple targets**: Decide which machine to push based on each target's `description` field autonomously; only ask the user when genuinely uncertain.

**Implementation flow**:

```
1. Resolve the target, get the full configuration of that target
2. Lightweight connectivity probe: ssh -o ConnectTimeout=3 {host} "echo ok"
   ├── Failed → Return immediately, trigger net_diagnose
   └── Success → Continue
3. Determine the sync source path:
   ├── target.local_subpath = null → source is {local_path}/
   └── target.local_subpath = "data_pipeline/" → source is {local_path}/data_pipeline/
4. Build exclude rules and write them to a temp file:
   ├── Hardcoded built-in excludes (see list below)
   ├── Read .gitignore from the project root (if respect_gitignore=true), convert to rsync filters per the MVP supported boundaries:
   │   ├── Ignore blank lines and comments (#)
   │   └── Convert negated patterns (!pattern) into rsync include rules (+ pattern); convert the rest into exclude rules (- pattern)
   └── Merge target.sync.extra_excludes into the temp file for rsync --exclude-from
5. Execute rsync:
   rsync -avz --delete \
     --exclude-from=<tmpfile> \
     {source_path}/ {ssh_host}:{remote_path}
6. Return: number of files transferred, elapsed time, last 5 lines of rsync output
```

**Hardcoded built-in excludes** (regardless of what `.gitignore` says, these are always excluded):

```
.git/
.DS_Store
__pycache__/
*.pyc
*.pyo
.idea/
.vscode/
node_modules/
.pytest_cache/
*.egg-info/
dist/
build/
.mypy_cache/
.ruff_cache/
.nomad.json
```

**Security constraints**:
- `--delete` is only allowed to take effect within the `remote_path` whitelist prefix
- `--delete` requires sufficient `remote_path` depth; acting on `/root`, `/home/user`, or other home root directories is forbidden
- If `source_path` does not exist or is not a directory, refuse to execute
- `.gitignore` MVP only commits to converting common rules; full Git semantics may be extended later via a parser, see `08-implementation-spec.md` for the specific boundaries

**Description (AI-facing)**:
> [High-cost operation] Incrementally sync local code to the specified target's remote server. In multi-target scenarios, the AI should autonomously decide which machine to sync based on each target's `description`; ask the user only when it cannot determine this. local_subpath is read automatically to decide which subdirectory to sync. SSH connectivity is checked before execution.

---

### `sync_pull` (remote → local)

**When triggered**: The remote has produced artifacts that need local analysis (test reports, crash samples, build artifacts, model checkpoints).

**Parameters**:
- `remote_relative_path: str`: path relative to that target's `remote_path` (file or directory)
- `target: str = "default"`: which machine to pull from
- `local_dest: str` (optional): local save path; defaults to `local_path/remote_artifacts/{target_name}/`

**Implementation**:
```bash
rsync -avz {ssh_host}:{remote_path}/{remote_relative_path} {local_dest}
```

**Security constraints**:
- `remote_relative_path` must be a path relative to `remote_path`; absolute paths, `..`, and null bytes are forbidden
- `local_dest` is written to `{cwd}/remote_artifacts/{target_name}/` by default
- If the user specifies `local_dest`, it must resolve to inside the current project directory; writing outside the project is forbidden
- The tool only returns the transfer summary, file sizes, and the local save path; large file contents are not returned via MCP

**Description (AI-facing)**:
> Pull a file or directory produced on the specified target's remote back to the local machine. Useful for analyzing remote test reports, crash samples, training checkpoints, and other artifacts. Just specify a path relative to the remote project directory.

---

## IV. Long Task Management Module (tmux state machine)

For remote tasks that take longer than 60 seconds and must not block the AI.

### Session Naming Convention

Format: `{project_name}_{target_name}_{task_slug}`, e.g. `ml-system_gpu_train`

- Includes `target_name` to ensure that tasks with the same name on different targets don't collide
- `task_slug` is specified by the caller (the AI), only `[a-z0-9-_]` allowed

---

### `task_start`

**Parameters**:
- `task_name: str`: task identifier
- `cmd: str`: command to run on the remote
- `target: str = "default"`: which machine to run on

**Implementation flow**:

```
1. Resolve the target, get ssh_host
2. Build the session name: {project_name}_{target_name}_{task_name}
3. Check whether the session already exists: ssh {host} "tmux has-session -t {session}"
   ├── Exists → Return "task already running" with current status, do not create a duplicate
   └── Not exists → Create a new session
4. Create the session (wrap the command in Base64 to defend against quote conflicts):
   a. Locally Base64-encode {cmd} to get {b64_cmd}
   b. Rebuild and chmod the execution script in the remote task directory:
      task_dir={remote_path}/.nomad/tasks
      ssh {host} "mkdir -p {task_dir} && echo {b64_cmd} | base64 -d > {task_dir}/{session}.sh && chmod +x {task_dir}/{session}.sh"
   c. Launch a tmux session to run this wrapper script:
      ssh {host} "tmux new-session -d -s {session} 'bash {task_dir}/{session}.sh 2>&1 | tee {task_dir}/{session}.log; echo \$? > {task_dir}/{session}.exit_code'"
5. Return immediately (non-blocking): session name, log path
```

**Key design**:
- The session name includes `target_name`, so the same project running tasks with the same name on different machines won't collide
- When the command exits, the exit code is forcibly written to `{remote_path}/.nomad/tasks/{session}.exit_code`, preventing state loss after abnormal exit
- Only if the remote project directory is not writable does it fall back to `/tmp/nomad/{project_name}/`

**Description (AI-facing)**:
> [Async long task] Launch a background task in the specified target's remote tmux; returns immediately without blocking. Suitable for long-running tasks such as compiling, training, and long fuzzing campaigns. After launch, poll progress with task_status. If a task with the same name is already running, it alerts rather than creating a duplicate.

---

### `task_status`

**Parameters**: `task_name: str`, `target: str = "default"`, `tail_lines: int` (default 10)

**Behavior**:
1. Check whether the session exists (`tmux has-session`)
2. Read `{session}.exit_code` in the task directory (if it exists, the task has ended)
3. Read the last N lines of `{session}.log` in the task directory

**Return status enum**:
- `running`: session exists, no exit_code file
- `finished_success`: exit_code is 0
- `finished_error`: exit_code is non-zero
- `missing`: session, log, and exit_code all absent
- `unknown`: SSH failed or state files are inconsistent

**Description (AI-facing)**:
> Query the current status and latest log of a remote tmux task on the specified target. Used for proactive polling or to respond to the user asking "how's it going?".

---

### `task_list`

**Parameters**: `target: str | None = None` (None means list tasks across all targets)

**Behavior**: Lists all tmux sessions on the remote prefixed with `{project_name}_`, grouped by target, with each session's live status attached.

**Purpose**: Prevents the AI from creating duplicates that pile up sessions; the AI should check before launching a new task.

---

### `task_kill`

**Parameters**: `task_name: str`, `target: str = "default"`

**Behavior**:
1. `ssh {host} "tmux kill-session -t {session}"`
2. By default, retains logs and exit_code for audit; an explicit cleanup parameter may be added later

---

## V. Network and Tunnel Module

### `tunnel_start`

**Parameters**: `target: str = "default"`

**When triggered**:
- The target has `network.reverse_tunnel.enabled=true`
- The remote needs to access the external network, pull dependencies, download models, or call APIs
- Before `task_start` launches a long task, if the target needs a proxy but the tunnel isn't running

**Behavior**:
1. Resolve the target, read `ssh_host`, `remote_bind_port`, `local_proxy_port`, `proxy_scheme`
2. Check whether the remote `127.0.0.1:{remote_bind_port}` is already occupied
3. Launch a persistent reverse tunnel using a dedicated SSH master:
   ```bash
   ssh -f -N -M \
     -S /tmp/nomad_tunnel_<hash> \
     -o ExitOnForwardFailure=yes \
     -o ServerAliveInterval=30 \
     -o ServerAliveCountMax=3 \
     -R 127.0.0.1:{remote_bind_port}:127.0.0.1:{local_proxy_port} \
     {ssh_host}
   ```
4. Return the tunnel status and the proxy environment variables the remote should use

**Description (AI-facing)**:
> Establish a persistent reverse tunnel on the specified target so that remote commands and long tmux tasks can reuse the local proxy via the remote `127.0.0.1:{remote_bind_port}`. SSH must already be reachable; this tool does not fix SSH networking.

---

### `tunnel_status`

**Parameters**: `target: str = "default"`

**Behavior**:
1. Check the tunnel master using `ssh -S /tmp/nomad_tunnel_<hash> -O check {ssh_host}`
2. Optionally execute the remote `nc -z 127.0.0.1 {remote_bind_port}` to check the port
3. Return one of `running`, `stopped`, `unknown`, plus a summary of the proxy environment variables

**Description (AI-facing)**:
> Check whether the persistent reverse tunnel for a target is still usable. Call before launching long tasks that depend on the proxy, or when troubleshooting network issues.

---

### `tunnel_stop`

**Parameters**: `target: str = "default"`

**Behavior**:
1. Close the tunnel master using `ssh -S /tmp/nomad_tunnel_<hash> -O exit {ssh_host}`
2. Does not kill tmux tasks; does not clean up task logs

**Description (AI-facing)**:
> Close the persistent reverse tunnel for a target. This only stops the proxy channel; it does not stop remote tasks.

---

### `net_diagnose`

**Parameters**: `target: str = "default"`

**When triggered**: Called proactively by the AI when rsync / SSH connection fails, or when the user says "why can't I connect".

**Behavior**:
1. Resolve the target's `ssh_host`, get the actual IP (`ssh -G {ssh_host}` to read the Hostname field)
2. Test direct connection: `nc -zv {ip} 22 -w 3`
3. If a local proxy is detected, test the path through the proxy
4. Read the `ssh -G {ssh_host}` output (parse the actual parameters for that Host from the local SSH config)
5. Check the `ALL_PROXY` / `HTTP_PROXY` environment variables

**Returns**: A structured diagnostic report, including direct-connection results, proxy-path results, an SSH config summary, and suggested actions.

**Description (AI-facing)**:
> Diagnose network connectivity from the local machine to the specified target server. Call when SSH or rsync fails, to help determine whether it's a TUN proxy route conflict, an SSH configuration error, or the network simply being unreachable.

---

## Tool Call Sequence Examples

### Scenario 1: Single server, run a training task

```
User: "Help me push the latest code and run a training pass"
  ↓
AI calls sync_push(target="default")
  → Sync succeeded, 12 files → gpu server
  ↓
AI calls task_list(target="gpu")
  → No active tasks
  ↓
If the target has reverse_tunnel enabled:
  AI calls tunnel_status(target="gpu")
    → If not running, call tunnel_start(target="gpu")
  ↓
AI calls task_start(task_name="train", cmd="python train.py", target="gpu")
  → Task launched in remote tmux [ml-system_gpu_train]
  ↓
(A few minutes later, the user asks "how's it going?")
  ↓
AI calls task_status(task_name="train", target="gpu", tail_lines=15)
  → status: running, latest log: "Epoch 3/10, loss=0.342..."
```

### Scenario 2: Multiple servers, push different modules separately

```
User: "Help me push the data processing module to data-server, and push the model code to gpu to run"
  ↓
AI calls sync_push(target="data-server")
  → Sync data_pipeline/ → data-server, 8 files
  ↓
AI calls sync_push(target="gpu")
  → Sync the whole project → gpu, 23 files
  ↓
AI calls run_remote(cmd="python pipeline.py", target="data-server")
  → Data processing starts...
  ↓
If the gpu target has reverse_tunnel enabled:
  AI calls tunnel_start(target="gpu")
  ↓
AI calls task_start(task_name="train", cmd="python train.py", target="gpu")
  → Training hangs in the background on gpu [ml-system_gpu_train]
```
