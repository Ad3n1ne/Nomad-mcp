# nomad — Project Overview

## One-line Definition

A local MCP Server that gives AI Agents the ability to perceive local network topology, intelligently sync code to remote hosts, and manage long-running remote tasks — fully bridging the "write code locally + verify and run remotely" Agentic development workflow.

---

## Background and Motivation

### The Core Pain Point

Modern AI development paradigms (Cursor Agent, Claude Code, Trae, etc.) only perceive the local filesystem by default. Once a developer's runtime environment is on a remote Linux server (GPU machines, VPS, lab clusters), the AI's "perceive → decide → execute → feedback" loop is severed at the **execution** stage:

- The AI runs commands locally, but the dependencies, drivers, and kernel modules are all on the remote host
- The AI doesn't know whether the local TUN proxy will interfere with the SSH / rsync route
- The AI modifies local code but forgets it hasn't been pushed — the remote is still running the old version
- For long compile/training tasks, the AI's tool call directly times out and gets killed

### First Principles

> There is nothing mysterious about remote development:
> **Edit text locally → trigger sync (rsync) → execute commands remotely (`ssh host "cmd"`).**
>
> The sole mission of this MCP Server is to wrap these three things into tools that an AI Agent can call seamlessly.

---

## Non-Goals (what we explicitly won't do)

- ❌ Not a general-purpose remote ops / multi-host management tool (doesn't compete with `mcp-ssh-manager`)
- ❌ No Web UI or visual interface
- ❌ No continuous integration / CD pipelines
- ❌ No Docker / K8s container orchestration layer
- ❌ Does not replace Git, does not manage version history

---

## Target Users

**Developers who rely heavily on AI Agents for development, and whose runtime environment is on a remote Linux server.** Typical scenarios:

| Type | Scenario |
|---|---|
| AI / Algorithm Engineer | Writes code on a local Mac/PC, pushes to a GPU server to run training |
| Security Researcher / Systems Developer | Needs to compile and verify under specific kernel / network environments |
| Developers restricted by corporate intranets | Bastion hosts / proxies block VS Code Remote, leaving only the command line |

**Common traits**: runs a local proxy (TUN / global), the remote is a Linux box, uses Agent mode for coding every day.

---

## Overall Architecture

```
┌─────────────────────────────────────────────────┐
│              Local Dev Machine (Your Computer)   │
│                                                 │
│  ┌────────────────┐   MCP Protocol (stdio)      │
│  │   AI Client    │ ◄─────────────────────────► │
│  │ (Cursor /      │                             │
│  │  Claude Code)  │                             │
│  └────────────────┘                             │
│                          ┌────────────────────┐ │
│                          │  nomad             │ │
│                          │   MCP Server       │ │
│                          │  (Python process)  │ │
│                          └────────┬───────────┘ │
│                                   │             │
│  ┌──────────────────┐   read      │             │
│  │ .nomad.json      │ ◄───────────┤             │
│  │ (project ID)     │             │             │
│  └──────────────────┘             │             │
│                                   │             │
│  ┌──────────────────┐   read      │             │
│  │ ~/.ssh/config    │ ◄───────────┤             │
│  └──────────────────┘             │             │
│                                   │             │
│  ┌──────────────────┐   read      │             │
│  │ .gitignore       │ ◄───────────┤             │
│  └──────────────────┘             │             │
└───────────────────────────────────┼─────────────┘
                                    │ SSH / rsync
                                    ▼
                      ┌─────────────────────────┐
                      │     Remote Linux Server │
                      │                         │
                      │  tmux sessions          │
                      │  .nomad/tasks/<task>.log│
                      │  .nomad/tasks/<task>.*  │
                      │  ~/workspace/project/   │
                      └─────────────────────────┘
```

---

## Document Index

| File | Content |
|---|---|
| `01-schema.md` | Complete Schema definition of `.nomad.json` |
| `02-tools.md` | Full toolset design (initialization, command execution, sync, long tasks) |
| `03-network.md` | Network topology handling (TUN awareness, reverse tunnels, bastion hosts) |
| `04-security.md` | Security sandbox (command blacklist, path whitelist, audit log) |
| `05-context-defense.md` | Context token defense (output truncation, noise filtering) |
| `06-workspace-isolation.md` | Workspace isolation mechanism |
| `07-dev-plan.md` | Phased development plan, technology choices, risk mitigations |
| `08-implementation-spec.md` | Pre-coding implementation spec (return contracts, state machine, security boundaries, testing requirements) |
