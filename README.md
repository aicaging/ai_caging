# Caging — Lightweight Manual Authorization Gateway

Caging is a lightweight web service that acts as a **gatekeeper** for potentially dangerous operations (`rm`, `format`, file unlocks, etc.) and privileged resource access. It screens every request through a chain of **policy rules**, **AI risk assessment**, and **manual human review** before execution.

> **Core philosophy**: No dangerous operation should execute without explicit authorization. Caging layers create a permission tree where requests escalate upward until approved.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    ROOT Layer (service)                      │
│  System user: root, port 58000                               │
│  • Runs as root → can chattr +i directly                     │
│  • Final authority for all escalated requests                │
│  • Also serves as parent for cadmin                          │
└───────────────────────────┬──────────────────────────────────┘
                            │ HTTP + X-API-Key (parent auth)
┌───────────────────────────┴──────────────────────────────────┐
│                   CADMIN Layer (service)                     │
│  System user: cadmin, port 58001                             │
│  • Admin dashboard + manual review UI                        │
│  • Policy evaluation + AI screening                          │
│  • Escalates to root when higher privileges needed           │
│  • Serves as parent for cage workspaces                      │
└───────────────────────────┬──────────────────────────────────┘
                            │ HTTP + X-API-Key (parent auth)
┌───────────────────────────┴──────────────────────────────────┐
│                   CAGE1 Workspace (cage)                     │
│  Linux user (no service process)                             │
│  • Submits commands via cagingcli.py to cadmin               │
│  • Protect/release files through parent layers               │
│  • Cannot chattr directly → always escalates                 │
└──────────────────────────────────────────────────────────────┘
```

### Multi-Layer Hierarchy

Caging is designed as a **tree of permission layers**. Each layer:

| Role | Description |
|------|-------------|
| **Root** | Runs as `root` (UID 0). Has native `chattr` capability. Final approval authority. |
| **Service** (admin) | Runs as a non-root user. Hosts the web dashboard. Evaluates policy, runs AI screening, and escalates when privileges are insufficient. |
| **Cage** (workspace) | Plain user without a service process. Connects to its parent via `cagingcli.py`. All operations go through the parent layer. |

Key principles:
- **Each layer has its own config, policy rules, and database.**
- **Child layers authenticate to parents via API keys.**
- **Requests escalate upward** when policy demands it or when the current layer lacks privileges.
- **Parent services must be restarted** after adding child API keys to pick up new config.

### Request Lifecycle

```
Client submits request (CLI/API)
         │
         ▼
  ┌─ Policy Engine evaluates rules (first match wins)
  │    • allow       → execute immediately
  │    • deny        → reject with reason
  │    • escalate    → forward to parent
  │    • require_human → queue for manual review
  │    • ai          → AI screening, then decide
  └────────────────────────────────────────
         │
         ▼ (if require_human)
  ┌─ Manual Review (Web Dashboard)
  │    • Single or dual approval
  │    • Delegate to another reviewer
  │    • AI risk score shown as advisory
  └────────────────────────────────────────
         │
         ▼ (if approved)
  ┌─ Executor runs command with timeout
  │    • subprocess with shell=False
  │    • Script source written to temp file
  │    • stdout/stderr/returncode captured
  └────────────────────────────────────────
         │
         ▼
  ┌─ Result returned to client
  │    • Callback URL notified (if configured)
  │    • WebSocket push for dashboard
  └────────────────────────────────────────
```

**State machine**: `pending` → `awaiting_review` → `first_approved` (dual) → `approved` → `executing` → `completed`/`failed`/`rejected`/`expired`/`escalated`

---

## Quick Start

```bash
# 1. Clone and install
cd caging
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure your topology
cp plan.yaml.sample plan.yaml
vim plan.yaml
** Setup AI API key , parent hosting params **

# 3. Deploy
sudo ./init.sh

# 4. Test from a cage workspace
sudo -u cage1 ./.caging/cli/exec.sh -t test "echo \"Hello Caging\""
sudo -u cage1 ./.caging/cli/exec.sh -t test "cat /etc/hostname"
```

---

## Installation

### Prerequisites

- **Linux** with `systemd` and `chattr` support
- **Python 3.10+** with `pip`
- **Root access** (sudo) for deployment

Dependencies (installed via `pip`):
```
fastapi, uvicorn, bcrypt, pyyaml, jinja2, websockets, apscheduler
```

### Plan-Based Deployment

The recommended way to deploy a full topology. Define all layers in `plan.yaml` and deploy at once:

```bash
sudo ./init.sh
```

Services run directly on localhost — no nginx needed:

| Service | URL |
|---------|-----|
| root    | `http://localhost:58000/ui/login` |
| cadmin  | `http://localhost:58001/ui/login` |

On a remote host, replace `localhost` with the host IP/domain.

### Nginx Portal + SSL (optional)

Optionally deploy an nginx reverse proxy to unify access on a single port with path-based routing:

```bash
sudo ./init.sh -nginx [--portal-port 50080]
```
This creates `/etc/nginx/sites-available/caging` (symlinked from `sites-enabled/caging`) with:
- `http://<host>:50080/root/` → root service (port 58000)
- `http://<host>:50080/cadmin/` → cadmin service (port 58001)
- Cookie path isolation to prevent session collisions
- WebSocket upgrade support for real-time dashboard
** Adding SSL yourself with nginx **

## CLI Usage and Example

Three convenience shell scripts wrap `cagingcli.py` with sensible defaults (`--await` + auto-detect config):

### `exec.sh` — Execute a command through the caging chain

```
exec.sh [-t topic] <command> [args...]
```

Examples:
```bash
# Escalate a command that needs parent credentials (e.g. DB DELETE)
exec.sh -t db/maintain 'mysql -u {{params.db_auth}} -e "DELETE FROM logs WHERE ts < NOW() - INTERVAL 30 DAY"'

# Escalate a privileged command to root
exec.sh -t service/start "systemctl restart nginx"

# Simple read-only command
exec.sh -t read "cat /etc/hosts"
```

> **How placeholders work**: `{{key}}` or `{{section.key}}` in any command is replaced from the executing layer's `config.yaml` before execution. If a key is missing, the request auto-escalates to the parent layer. Config params are defined in `plan.yaml` under each layer's `params:` section.

### `protect.sh` — Protect a file/folder from updates and deletion

```
protect.sh [-t topic] <path>
```

### `release.sh` — Request to release a protected file/folder

```
release.sh [-t topic] [--reason "..."] <path>
```


## Human review Usage and Example

### Web Dashboard

Access the admin dashboard through the nginx portal (if configured) or directly:

| Access | URL |
|--------|-----|
| Direct (cadmin) | `http://<host>:58001/ui/login` |
| Direct (root) | `http://<host>:58000/ui/login` |

Login with the username/password set during deployment in `plan.yaml`.

Select a request task and use AI for risk analysis. Supported built-in commands :

Several examples:
| Command | Action |
|---------|--------|
| `>a` or `>approve` | Approve the request |
| `>r` or `>reject` | Reject the request |
| `?h` or `?help` | List all built-in commands |
| `?r` or `?risk` | Ask AI for risk analysis |

Any input without a built-in command prefix is treated as a chat message and sent to the AI for reasoning on current request.

## Caged AI

Run an AI agent (e.g. open-source [GenericAgent](https://github.com)) inside a caged Linux user — **no sudo, no DB DELETE permission**. When the AI needs elevated privileges, it calls `exec.sh` / `protect.sh` / `release.sh` as tools, and caging escalates the request to the parent layer for approval and credential substitution.

### How it works

```
Cage1 user (AI agent runs here)
  │  No sudo. DB user has SELECT only — no DELETE/DROP.
  │
  ├─ exec.sh -t db/maintain 'mysql -u {{params.db_auth}} -e "DELETE ..."'
  │     │
  │     ▼
  │  cadmin (parent) — substitutes {{params.db_auth}} → admin_user:admin_pass
  │     │  Evaluates policy, runs AI risk screening, queues for human review
  │     ▼
  │  root — executes with cadmin's DB credentials (full DELETE permission)
  │
  ├─ protect.sh /some/config.yaml     → escalates to root → chattr +i
  └─ release.sh /some/config.yaml     → escalates to root → chattr -i
```

### Tool mapping for AI agents

Configure or prompt your AI agent to invoke these shell scripts as external tools:

| Action | Command | Notes |
|--------|---------|-------|
| Execute privileged command | `exec.sh -t <topic> <cmd>` | Uses `{{params.*}}` for parent credentials |
| Lock a file | `protect.sh <path>` | Only root can `chattr +i`; always escalates |
| Unlock a file | `release.sh <path>` | Always escalates to parent for approval |

### Example: AI agent running in cage1

```bash
# 1. Start the AI agent as cage1 user (no sudo, limited DB user)
sudo -u cage1 genericagent run

# 2. AI agent wants to clean old logs — needs DELETE permission it doesn't have
#    It calls:
exec.sh -t db/maintain 'mysql -u {{params.db_auth}} -e "DELETE FROM logs WHERE ts < NOW() - INTERVAL 30 DAY"'

# 3. caging flow:
#    cage1 → cadmin ({{params.db_auth}} → admin_user:admin_pass) → root → executes DELETE

# 4. AI agent modifies a config, then locks it:
protect.sh /etc/myapp/config.yaml

# 5. Later, needs to update — requests release:
release.sh --reason "Quarterly config update" /etc/myapp/config.yaml
```

> **Key insight**: The AI agent itself has zero privileged access. All dangerous operations flow through the caging authorization chain — policy rules, AI risk screening, and human review — before execution.