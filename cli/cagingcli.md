# Caging CLI — User Guide

`cagingcli.py` is a command-line client for the parent Caging service. It reads connection settings from `cagingcli.yaml`.

---

## Quick Start

```bash
cagingcli.py health                      # check service status
cagingcli.py exec echo hello             # run a command
cagingcli.py list pending                # list pending requests
cagingcli.py status abc123               # check a request
```

---

## Setup

Deployed automatically by `init.sh`. Files land in the child user's data dir:

```
/home/caging/<user>/.caging/
├── cagingcli.py
└── cagingcli.yaml        # base_url + api_key (auto-generated)
```

Minimal manual config:

```yaml
# cagingcli.yaml
parent:
  base_url: "http://parent:8000"
  api_key: "ak_live_..."
  timeout: 30
defaults:
  output: text
```

---

## Global Options

| Option            | Description                                   |
|-------------------|-----------------------------------------------|
| `-c, --config`    | Config file path (default: `./cagingcli.yaml`)|
| `--server URL`    | Override `base_url`                           |
| `--api-key KEY`   | Override API key                              |
| `-t, --topic`     | Topic label for routing/grouping (default: `na`) |
| `-o, --output`    | Output format: `text` or `json`               |

---

## Commands

### `exec` — Execute a Command

```bash
cagingcli.py exec <command...> [--timeout N] [--await] [--script FILE] [--catalog X] [--env K=V]
```

| Option              | Default | Description                              |
|---------------------|---------|------------------------------------------|
| `--timeout SEC`     | 60      | Execution timeout                        |
| `--script FILE`     | —       | Execute a script file                    |
| `--catalog LABEL`   | ""      | Category label                           |
| `--env KEY=VAL`     | —       | Env var (repeatable)                     |
| `--dual-approval`   | false   | Require dual approval                    |
| `--assigned-reviewer ID` | "" | Specific reviewer                        |
| `--await, -A`       | —       | Poll until completed (see below)         |
| `--await-timeout`   | 300     | Max poll seconds with `--await`          |

Examples:

```bash
cagingcli.py exec echo hello world
cagingcli.py exec --timeout 120 sleep 30
cagingcli.py exec --script ./deploy.sh
cagingcli.py exec --env PYTHONPATH=/app python test.py
cagingcli.py exec --await --catalog deploy ./update.sh     # wait for result
cagingcli.py -t mytopic exec date                           # tag with topic
```

---

### `protect` — Protect a File (make read-only)

```bash
cagingcli.py protect <path> [--await]
```

Examples:

```bash
cagingcli.py protect /app/data/db.sqlite
cagingcli.py -t configs protect /app/conf.yaml --await
```

---

### `release` — Release a Protected File

```bash
cagingcli.py release <path> [--reason TEXT] [--await]
```

Examples:

```bash
cagingcli.py release /app/data/db.sqlite --reason "update"
```

---

### `firewallcli.sh` — Reverse Firewall Management

Wrapper around `reverse_firewall.sh` via `exec.sh`. Manages per-user outbound traffic restrictions using iptables.

```bash
firewallcli.sh enable  <user> [IP/domain/CIDR...]
firewallcli.sh disable <user>
firewallcli.sh add     <user> <IP/domain/CIDR...>
firewallcli.sh list    <user>
```

| Subcommand | Description |
|------------|-------------|
| `enable`   | Activate firewall with whitelist (block all other outbound) |
| `disable`  | Remove all firewall rules for the user |
| `add`      | Append entries to the whitelist and re-apply |
| `list`     | Display the current whitelist |

Whitelist supports: IPv4 address, CIDR network, domain name (resolved at apply time).

Examples:

```bash
firewallcli.sh enable cage1 api.deepseek.com
firewallcli.sh add cage1 github.com
firewallcli.sh list cage1
firewallcli.sh disable cage1
```

---

### `status` — Check Request Status

```bash
cagingcli.py status <request_id>
```

---

### `list` — List Requests

```bash
cagingcli.py list [status] [--limit N]
```

Status filter: `pending`, `executing`, `awaiting_review`, `approved`, `rejected`, `completed`, `failed`, `escalated`, `all`.

Examples:

```bash
cagingcli.py list
cagingcli.py list pending
cagingcli.py list failed --limit 5
cagingcli.py -o json list completed
```

Text output:

```
  [       completed] exec      622be59ac305
  [          failed] exec      8ee9d62cc22e
  --- 2 items ---
```

---

### `health` — Service Health

```bash
cagingcli.py health
```

### `parent` — Show Parent Connection Info

```bash
cagingcli.py parent              # prints config source + effective settings
```

---

## `--await` / `-A` Polling

When a command enters async flow (e.g. awaiting review), `--await` makes the CLI poll `/status/{id}` until the request reaches a terminal state:

```bash
cagingcli.py exec --await risky-command   # polls every 2s, prints spinner
```

- Default timeout: **300s** (override with `--await-timeout`)
- Press Ctrl+C to stop polling; request continues on the server
- Check later with `cagingcli.py status <id>`

Terminal states: `completed`, `failed`, `rejected` → exits immediately; `TIMEOUT` → exits 1.

Available for: `exec`, `protect`, `release`.

---

## Output Formats

- **text** (default) — human-readable with icons: ✓ completed, ✗ failed/rejected, ⏳ awaiting_review, ▶ executing, ○ pending, ↗ escalated
- **json** — machine-readable, for scripting:

```bash
cagingcli.py -o json health | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])"
```

---

## Exit Codes

- `0` — success (completed)
- `1` — error, rejected, failed, timeout, or connection problem

---

## Examples

### Wait for a request (--await)

```bash
# Submit and wait
cagingcli.py exec --await --timeout 120 ./long-job.sh

# With custom poll timeout
cagingcli.py exec --await --await-timeout 600 ./migration.sh
```

### Request chain (cage1 → cadmin → root)

```bash
cagingcli.py -c /home/caging/cage1/.caging/cagingcli.yaml exec date
cagingcli.py -c /home/caging/cadmin/.caging/cagingcli.yaml list pending
```

### Protect + release workflow

```bash
cagingcli.py protect /app/config.ini
cagingcli.py release /app/config.ini --reason "update"
# edit file, then re-protect
cagingcli.py protect /app/config.ini
```

---

## Troubleshooting

| Symptom                      | Cause                            | Fix                                 |
|------------------------------|----------------------------------|-------------------------------------|
| No `base_url` configured     | Config missing or empty          | Create `cagingcli.yaml` or `--server`|
| No API key configured        | Key not set                      | Add API key or use `--api-key`       |
| Connection failed            | Parent unreachable               | Check URL, verify service is up      |
| 403 Invalid API key          | Wrong/revoked key                | Regenerate key                       |
| exec → rejected              | Not in allowlist                 | Check parent `policy.yaml`           |
