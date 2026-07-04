# Network Topology Handling

---

## Problem Background

The local TUN proxy is the reason this tool came into existence, and it's also the most unstable factor. The core contradictions:

- Under TUN global mode, traffic destined for the remote server may **be taken over by the proxy**, causing SSH latency jitter or the connection being interrupted by the proxy node
- Some intranet server IP ranges **should not go through the proxy**, but a global TUN may capture them by mistake
- Locally you can reach the external network freely, **but the remote can't pull dependencies** (because the remote has no proxy)

---

## Local Network State Snapshot

The MCP Server reads the following information at startup (and before each tool call) to build a network snapshot:

```python
PROXY_ENV_KEYS = ["ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "http_proxy", "https_proxy"]

def get_local_network_snapshot() -> dict:
    proxy = None
    for key in PROXY_ENV_KEYS:
        if val := os.environ.get(key):
            proxy = val
            break
    return {
        "proxy_detected": proxy is not None,
        "proxy_url": proxy,       # e.g. "http://127.0.0.1:7890"
        "proxy_port": ...,        # extracted from proxy_url
    }
```

---

## Three SSH Connection Strategies

Determined by the `network` configuration in `.nomad.json`:

### Strategy 1: Direct connection (default)

```python
# use_proxy_for_ssh = false, jump_host = null
cmd = f"ssh -o ConnectTimeout=5 -o BatchMode=yes {ssh_host} ..."
```

Applies when: the remote server IP does not go through the TUN, or the TUN rules already exclude that IP.

### Strategy 2: Via a bastion host (jump host)

```python
# jump_host is not null
cmd = f"ssh -J {jump_host} -o ConnectTimeout=5 {ssh_host} ..."
```

Applies when: an intranet server needs to be reached through a bastion host. `jump_host` also reads its alias from `~/.ssh/config`.

### Strategy 3: Via the local proxy

```python
# use_proxy_for_ssh = true
# Injected via -o ProxyCommand; the proxy parameters must be parsed from the local network snapshot
cmd = [
  "ssh",
  "-o", "ProxyCommand=nc -X 5 -x 127.0.0.1:{proxy_port} %h %p",
  ssh_host,
  "..."
]
```

Applies when: the SSH connection itself must go through a proxy to reach the remote.

> MVP constraint: `jump_host` and `use_proxy_for_ssh=true` are not auto-combined. When both are configured at the same time, return `invalid_config`, to avoid the unclear semantics of mixing ProxyJump and ProxyCommand causing misdiagnosis. May be extended later into an explicit multi-hop strategy.

> Proxy parsing constraint: The local proxy must yield a scheme and a port from environment variables or initialization parameters. The MVP supports `socks5://`, `socks4://`, and a bare local-port form; other proxy types such as HTTP CONNECT return `ssh_proxy_unavailable` first, to avoid generating an unreliable ProxyCommand.

---

## Connectivity Probe (Pre-flight Check)

All SSH-related operations (`sync_push`, `run_remote`, `task_start`) must do a lightweight probe before executing:

```python
def probe_ssh(ssh_host: str, timeout: int = 3) -> bool:
    result = subprocess.run(
        ["ssh", "-o", f"ConnectTimeout={timeout}", "-o", "BatchMode=yes",
         ssh_host, "echo ok"],
        capture_output=True, text=True
    )
    return result.returncode == 0
```

**On probe failure**:
- Immediately circuit-break; do not hang waiting
- Return a structured error message, suggesting the AI call `net_diagnose`
- Never fail silently (letting rsync or SSH wait out their own timeouts)

---

## Reverse Tunnel

**Purpose**: Reverse-share the local TUN proxy capability with the remote server, so that the remote can use the local proxy to reach the external network when running `pip install`, `go get`, `apt`, etc.

**Trigger condition**: `network.reverse_tunnel.enabled = true` in `.nomad.json`

**Implementation**: Establish a dedicated persistent SSH connection via `tunnel_start`, with the `-R` flag attached:

```bash
ssh -f -N -R 127.0.0.1:{remote_bind_port}:127.0.0.1:{local_proxy_port} {ssh_host}
```

**Usage**: When executing commands on the remote, you need to set the proxy environment variables manually:

```bash
ALL_PROXY=socks5://127.0.0.1:{remote_bind_port} pip install torch
```

Or configure it uniformly in `runtime.extra_env`:

```json
"extra_env": {
  "ALL_PROXY": "socks5://127.0.0.1:7890"
}
```

**Notes**:
- The reverse tunnel is **long-connection only**; it works only while the SSH connection persists.
- **No-config security**: Because `-R` binds the remote loopback address `127.0.0.1`, the remote SSH server **does not need** `GatewayPorts yes`. As long as the remote program connects to its own loopback interface, it can reuse the proxy — no privileged configuration on the remote system is required.

> Important limitation: You cannot attach `-R` directly onto a short-lived connection like `ssh host "tmux new-session -d ..."` and assume the long task can keep using the proxy. Once the SSH command returns, the reverse tunnel also closes. If a long task needs the proxy, you must first establish a dedicated persistent reverse tunnel via `tunnel_start`, and only then launch the tmux task.

### Persistent Tunnel Lifecycle

nomad uses a dedicated SSH master connection to carry the reverse tunnel:

```bash
ssh -f -N -M \
  -S /tmp/nomad_tunnel_<hash> \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -R 127.0.0.1:{remote_bind_port}:127.0.0.1:{local_proxy_port} \
  {ssh_host}
```

Corresponding MCP tools:

| Tool | Purpose |
|---|---|
| `tunnel_start` | Establish a target-level persistent reverse tunnel |
| `tunnel_status` | Check whether the SSH master and the remote port are still usable |
| `tunnel_stop` | Close the tunnel; does not stop tmux tasks |

The tunnel is target-level state, not bound to a single task. `task_kill` does not stop the tunnel automatically; `tunnel_stop` does not kill tasks either.

---

## SSH Connection Reuse (ControlMASTER)

**Problem**: When MCP tools are called frequently (e.g. polling task status), each call initiates a new SSH handshake, adding extra latency and producing lots of short-connection records in logs/monitoring.

**Solution**: Reuse connections via SSH ControlMaster.

> [!WARNING]
> The UNIX domain socket file path length is limited to **104~108 bytes** on most UNIX systems. If you use the default `~/.ssh/...`, once your home directory is deep or the host alias is long, it's easy to exceed the limit and trigger SSH errors. That's why we use the system `/tmp/` to shorten the absolute path.

```python
# Use the OpenSSH %C hash, covering local host, remote host, port, remote user,
# while avoiding both an over-long Unix domain socket path and accidental reuse
# across different users connecting to the same host.
CONTROL_PATH = "/tmp/nomad_ssh_%C"

def build_ssh_args(ssh_host: str) -> list[str]:
    return [
        "ssh",
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={CONTROL_PATH}",
        "-o", "ControlPersist=60s",   # After the last use, keep the connection for 60 seconds
        "-o", "ConnectTimeout=5",
        "-o", "BatchMode=yes",
        ssh_host
    ]
```

Effect: consecutive calls to the same server share one TCP connection, dropping latency from ~300ms to ~10ms.

---

## Network Diagnostic Flow (net_diagnose implementation details)

```
1. Resolve the actual IP of ssh_host (via `ssh -G {ssh_host}` to get the Hostname field)
2. Direct connection test:
   nc -zv {ip} 22 -w 3
3. If a local proxy is detected:
   Test through the proxy: curl --proxy {proxy_url} --max-time 3 -s http://{ip}:22
4. Read the full output of `ssh -G {ssh_host}`, extracting key fields:
   - hostname (actual IP/domain)
   - port
   - proxycommand (if configured)
   - identityfile
5. Assemble the diagnostic report
```

**Example diagnostic report**:

```
Network Diagnostic Report [aliyun-gpu]
───────────────────────────
Actual address: 47.xxx.xxx.xxx:22
Direct test: ✅ Success (RTT: 12ms)
Proxy path: ⚠️  Not tested (no local proxy environment variables detected)
SSH config: User=root, IdentityFile=~/.ssh/id_ed25519

Suggestion: Direct connection is fine. If you still have issues, check the SSH key permissions (chmod 600).
```
