#!/usr/bin/env python3
"""Caging CLI — command-line client for parent caging service.

Usage:
    cagingcli.py [global-opts] <command> [args]

Configuration: reads cagingcli.yaml (default ./cagingcli.yaml)
"""

VERSION = "1.0.2"

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

try:
    import yaml
except ImportError:
    yaml = None

DEFAULT_CONFIG = "./cagingcli.yaml"


# ── Config ──────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    """Load cagingcli.yaml, return config dict or empty."""
    if yaml is None:
        print("⚠  PyYAML not installed, using defaults", file=sys.stderr)
        return {}
    try:
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
            return cfg
    except FileNotFoundError:
        print(f"⚠  Config not found: {path}, using defaults", file=sys.stderr)
        return {}
    except Exception as e:
        print(f"⚠  Config error: {e}", file=sys.stderr)
        return {}


def merge_config(file_cfg: dict, args) -> dict:
    """CLI args override file config."""
    parent = file_cfg.get("parent", {})
    defaults = file_cfg.get("defaults", {})

    return {
        "base_url": (args.server or parent.get("base_url", "")).rstrip("/"),
        "api_key": args.api_key or parent.get("api_key", ""),
        "timeout": parent.get("timeout", 30),
        "output": args.output or defaults.get("output", "text"),
    }


# ── HTTP ────────────────────────────────────────────────────────────────

def api_request(
    method: str,
    endpoint: str,
    body: dict = None,
    cfg: dict = None,
    timeout: int = 30,
) -> dict:
    """Send request to parent caging API."""
    if cfg is None:
        cfg = {}
    url = f"{cfg.get('base_url', '')}{endpoint}"
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": cfg.get("api_key", ""),
    }
    data = json.dumps(body).encode() if body else None

    try:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read()).get("detail", "")
        except Exception:
            detail = e.reason or ""
        return {"error": True, "status": e.code, "detail": detail}
    except urllib.error.URLError as e:
        return {"error": True, "status": 0, "detail": f"Connection failed: {e.reason}"}
    except Exception as e:
        return {"error": True, "status": -1, "detail": str(e)}


# ── Output ──────────────────────────────────────────────────────────────

def output_result(data, fmt: str = "text"):
    """Print result as text or JSON."""
    if fmt == "json":
        print(json.dumps(data, indent=2, default=str))
        return

    # --- list of items ---
    if isinstance(data, list):
        if len(data) == 0:
            print("  (empty)")
            return
        for i, item in enumerate(data):
            rid = item.get("id") or item.get("request_id") or f"#{i}"
            st = item.get("status", "?")
            rtype = item.get("type", item.get("request_type", "?"))
            print(f"  [{st:>16}] {rtype:8}  {rid}")
        print(f"  --- {len(data)} items ---")
        return

    # --- text output ---
    if data.get("error"):
        print(f"✗  Error [{data.get('status', '?')}]: {data.get('detail', 'unknown')}")
        return

    rid = data.get("request_id", "")
    status = data.get("status", "")
    reason = data.get("reason", "")

    if status == "escalated":
        print(f"↗  Escalated to parent")
        if reason:
            print(f"   Reason: {reason}")
        return

    if rid:
        icon = {"completed": "✓", "failed": "✗", "rejected": "✗",
                "awaiting_review": "⏳", "executing": "▶", "pending": "○",
                "escalated": "↗"}.get(status, "?")
        print(f"{icon}  Request: {rid}")
        print(f"   Status: {status}")
        if reason:
            print(f"   Reason: {reason}")

        result = data.get("result")
        if result:
            stdout = (result.get("stdout") or "").rstrip()
            stderr = (result.get("stderr") or "").rstrip()
            rc = result.get("returncode", "?")
            if stdout:
                print(f"   stdout:")
                for line in stdout.split("\n"):
                    print(f"     {line}")
            if stderr:
                print(f"   stderr:")
                for line in stderr.split("\n"):
                    print(f"     {line}")
            print(f"   returncode: {rc}")
        return

    # Health
    if "service" in data:
        svc_name = data.get("service", "Caging")
        if isinstance(svc_name, dict):
            svc_name = svc_name.get("name", "Caging")
        print(f"Service: {svc_name}")
        print(f"  Status: {data.get('status', 'ok')}")
        uptime = data.get("uptime", data.get("service", {}).get("uptime", "?")) if isinstance(data.get("service"), dict) else "?"
        print(f"  Uptime: {uptime}s")
        print(f"  Parent: {data.get('parent_enabled', False)}")
        return

    # Fallback
    print(json.dumps(data, indent=2, default=str))


# ── Polling helper for --await ─────────────────────────────────────────

def poll_request(cfg: dict, request_id: str, await_timeout: int = 300,
                  output_fmt: str = "text"):
    """Poll /status/{request_id} until completion. Used by --await flag."""
    poll_interval = 2
    deadline = time.time() + await_timeout
    spinner = ["⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"]
    si = 0
    last_status = "unknown"

    print(f"⏳ Request {request_id} — polling (--await, timeout {await_timeout}s)")
    print(f"   cagingcli.py status {request_id}  — check anytime", flush=True)

    while time.time() < deadline:
        si = (si + 1) % len(spinner)
        status_resp = api_request("GET", f"/status/{request_id}",
                                   cfg=cfg, timeout=10)
        cur_status = status_resp.get("status", "")

        if cur_status != last_status:
            print(f"\r{spinner[si]} Status: {cur_status}{' ' * 20}")
            last_status = cur_status

        if cur_status in ("completed", "failed"):
            print(f"\r✅  {cur_status.upper()}{' ' * 30}")
            exec_result = status_resp.get("execution_result") or {}
            if exec_result:
                out = exec_result.get("stdout", "")
                err = exec_result.get("stderr", "")
                rc = exec_result.get("returncode", -1)
                if out:
                    print(out)
                if err:
                    print(err, file=sys.stderr)
                if rc != 0:
                    sys.exit(1)
            else:
                output_result(status_resp, output_fmt)
            return

        if cur_status == "rejected":
            print(f"\r❌  REJECTED{' ' * 30}")
            output_result(status_resp, output_fmt)
            sys.exit(1)

        print(f"\r{spinner[si]} Status: {cur_status} (polling...)"
              f"{' ' * 10}", end="", flush=True)
        time.sleep(poll_interval)

    # Timeout
    print(f"\r⏰  TIMEOUT after {await_timeout}s"
          f" — request still in '{last_status}'{' ' * 20}")
    print(f"   Check later: cagingcli.py status {request_id}",
          file=sys.stderr)
    sys.exit(1)


# ── Commands ────────────────────────────────────────────────────────────

def cmd_exec(args, cfg: dict):
    """POST /exec"""
    payload = {
        "command": " ".join(args.args),
        "timeout": args.timeout,
        "catalog": args.catalog or "",
        "topic": args.topic,
        "dual_approval": args.dual_approval,
        "assigned_reviewer": args.assigned_reviewer or "",
    }
    if args.script:
        try:
            with open(args.script) as f:
                payload["script_source"] = f.read()
        except Exception as e:
            print(f"✗  Cannot read script {args.script}: {e}")
            sys.exit(1)
    if args.env:
        env_dict = {}
        for e in args.env:
            if "=" in e:
                k, v = e.split("=", 1)
                env_dict[k] = v
        payload["env"] = env_dict

    result = api_request("POST", "/exec", body=payload, cfg=cfg, timeout=cfg.get("timeout", 30))

    # Terminal statuses — output immediately
    term_status = result.get("status", "")
    if term_status in ("completed", "failed", "rejected", "executing"):
        output_result(result, cfg.get("output", "text"))
        if result.get("error") or term_status in ("rejected", "failed"):
            sys.exit(1)
        return

    # If --await is set, poll for completion
    if getattr(args, "await", False):
        request_id = result.get("request_id")
        if not request_id:
            output_result(result, cfg.get("output", "text"))
            sys.exit(1)
        poll_request(cfg, request_id,
                     await_timeout=getattr(args, "await_timeout", 300),
                     output_fmt=cfg.get("output", "text"))
        return

    # No --await: show initial response (e.g. "awaiting_review")
    output_result(result, cfg.get("output", "text"))
    if result.get("error"):
        sys.exit(1)


def cmd_protect(args, cfg: dict):
    """POST /protect"""
    payload = {"path": args.path, "topic": args.topic}
    result = api_request("POST", "/protect", body=payload, cfg=cfg, timeout=cfg.get("timeout", 30))

    term_status = result.get("status", "")
    if term_status in ("completed", "failed", "rejected"):
        output_result(result, cfg.get("output", "text"))
        if result.get("error") or term_status in ("rejected", "failed"):
            sys.exit(1)
        return

    if getattr(args, "await", False):
        request_id = result.get("request_id")
        if not request_id:
            output_result(result, cfg.get("output", "text"))
            sys.exit(1)
        poll_request(cfg, request_id,
                     await_timeout=getattr(args, "await_timeout", 300),
                     output_fmt=cfg.get("output", "text"))
        return

    output_result(result, cfg.get("output", "text"))
    if result.get("error"):
        sys.exit(1)


def cmd_release(args, cfg: dict):
    """POST /release"""
    payload = {"path": args.path, "reason": args.reason, "topic": args.topic}
    result = api_request("POST", "/release", body=payload, cfg=cfg, timeout=cfg.get("timeout", 30))

    term_status = result.get("status", "")
    if term_status in ("completed", "failed", "rejected"):
        output_result(result, cfg.get("output", "text"))
        if result.get("error") or term_status in ("rejected", "failed"):
            sys.exit(1)
        return

    if getattr(args, "await", False):
        request_id = result.get("request_id")
        if not request_id:
            output_result(result, cfg.get("output", "text"))
            sys.exit(1)
        poll_request(cfg, request_id,
                     await_timeout=getattr(args, "await_timeout", 300),
                     output_fmt=cfg.get("output", "text"))
        return

    output_result(result, cfg.get("output", "text"))
    if result.get("error"):
        sys.exit(1)


def cmd_status(args, cfg: dict):
    """GET /status/{request_id}"""
    result = api_request("GET", f"/status/{args.request_id}", cfg=cfg, timeout=cfg.get("timeout", 30))
    output_result(result, cfg.get("output", "text"))
    if result.get("error"):
        sys.exit(1)


def cmd_list(args, cfg: dict):
    """GET /requests?status=..."""
    params = {}
    if args.status and args.status != "all":
        params["status"] = args.status
    if args.limit:
        params["limit"] = str(args.limit)
    qs = urllib.parse.urlencode(params)
    endpoint = f"/requests?{qs}" if qs else "/requests"
    result = api_request("GET", endpoint, cfg=cfg, timeout=cfg.get("timeout", 30))
    if result.get("error"):
        output_result(result, cfg.get("output", "text"))
        sys.exit(1)
    items = result.get("requests", result)
    output_result(items, cfg.get("output", "text"))


def cmd_health(args, cfg: dict):
    """GET /health"""
    result = api_request("GET", "/health", cfg=cfg, timeout=cfg.get("timeout", 30))
    output_result(result, cfg.get("output", "text"))
    if result.get("error"):
        sys.exit(1)


def cmd_parent(args, cfg: dict):
    """Print parent connection info from cagingcli.yaml's parent: section."""
    # Read raw file to show what's actually in the YAML (not CLI overrides)
    file_cfg = load_config(args.config) if hasattr(args, 'config') else {}
    raw_parent = file_cfg.get("parent", {})

    print(f"Config file: {getattr(args, 'config', DEFAULT_CONFIG)}")
    print(f"Parent section from cagingcli.yaml:")
    print()

    if not raw_parent:
        print("  (empty or missing 'parent:' section)")
        return

    for key, value in raw_parent.items():
        if key == "api_key" and isinstance(value, str) and len(value) > 8:
            masked = value[:8] + "..." + value[-4:]
            print(f"  {key}: {masked}")
        else:
            print(f"  {key}: {value}")

    # Show parent_params — supported params that parent understands
    parent_params = file_cfg.get("parent_params", {})
    if parent_params:
        print()
        print("Parent-supported parameters (parent_params):")
        for key, desc in parent_params.items():
            print(f"  {key}: {desc}")
        print()
        print("# Available as {{parent_params.<key>}} placeholders")

    # Show effective values (post-merge, for transparency)
    print()
    print("Effective (merged with CLI overrides):")
    print(f"  base_url: {cfg.get('base_url', '')}")
    masked_effective = cfg.get("api_key", "")
    if len(masked_effective) > 8:
        masked_effective = masked_effective[:8] + "..." + masked_effective[-4:]
    print(f"  api_key: {masked_effective}")
    print(f"  timeout: {cfg.get('timeout', 30)}s")


# ── Main ────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cagingcli",
        description=f"cagingcli v{VERSION} — Command-line client for parent caging service.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  cagingcli.py health\n"
            "  cagingcli.py exec cat /etc/shadow\n"
            "  cagingcli.py exec --script ./deploy.sh --timeout 120\n"
            "  cagingcli.py protect /app/data/db.sqlite\n"
            "  cagingcli.py release /app/data/db.sqlite --reason 'done'\n"
            "  cagingcli.py status abc123\n"
            "  cagingcli.py list pending\n"
            "  cagingcli.py -o json exec cat /etc/hostname\n"
        ),
    )

    # Global options
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG,
                        help=f"Config file (default: {DEFAULT_CONFIG})")
    parser.add_argument("--server", help="Override parent base_url")
    parser.add_argument("--api-key", help="Override API key")
    parser.add_argument("-t", "--topic", default="na", help="Topic label (default: na)")
    parser.add_argument("-o", "--output", choices=["text", "json"],
                        help="Output format (default: text)")

    sub = parser.add_subparsers(dest="command", required=True)

    # exec
    p = sub.add_parser("exec", help="Execute a command on the parent layer")
    p.add_argument("args", nargs="+", metavar="command", help="Command and arguments")
    p.add_argument("--timeout", type=int, default=60, help="Command timeout (seconds)")
    p.add_argument("--script", help="Script file to execute instead of command")
    p.add_argument("--catalog", help="Catalog/category label")
    p.add_argument("--env", action="append", default=[], help="Environment variables (KEY=VAL)")
    p.add_argument("--dual-approval", action="store_true", help="Require dual approval")
    p.add_argument("--assigned-reviewer", help="Specific reviewer ID")
    p.add_argument("--await", "-A", action="store_true",
                   help="Wait and poll until request completes (for async/review flows)")
    p.add_argument("--await-timeout", type=int, default=300,
                   help="Max seconds to wait when --await is set (default: 300)")

    # protect
    p = sub.add_parser("protect", help="Protect a file (make read-only)")
    p.add_argument("path", help="File path to protect")
    p.add_argument("--await", "-A", action="store_true",
                   help="Wait and poll until request completes")
    p.add_argument("--await-timeout", type=int, default=300,
                   help="Max seconds to wait when --await is set (default: 300)")

    # release
    p = sub.add_parser("release", help="Release a protected file")
    p.add_argument("path", help="File path to release")
    p.add_argument("--reason", default="", help="Release reason")
    p.add_argument("--await", "-A", action="store_true",
                   help="Wait and poll until request completes")
    p.add_argument("--await-timeout", type=int, default=300,
                   help="Max seconds to wait when --await is set (default: 300)")

    # status
    p = sub.add_parser("status", help="Check request status")
    p.add_argument("request_id", help="Request ID")

    # list
    p = sub.add_parser("list", help="List requests by status")
    p.add_argument("status", nargs="?", default="all",
                   choices=["pending", "executing", "awaiting_review",
                            "approved", "rejected", "completed", "failed",
                            "escalated", "all"],
                   help="Filter by status (default: all)")
    p.add_argument("--limit", type=int, help="Max results")

    # health
    sub.add_parser("health", help="Check service health")

    # parent
    sub.add_parser("parent", help="Show parent connection info (for AI prompts)")

    # help (alias for -h/--help)
    sub.add_parser("help", help="Show this help message and exit")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    # help command doesn't need config
    if args.command == "help":
        print(f"cagingcli v{VERSION}")
        parser.print_help()
        return

    file_cfg = load_config(args.config)
    cfg = merge_config(file_cfg, args)

    if not cfg.get("base_url"):
        print("✗  No parent base_url configured. "
              "Set in cagingcli.yaml or use --server", file=sys.stderr)
        sys.exit(1)
    if not cfg.get("api_key"):
        print("✗  No API key configured. "
              "Set in cagingcli.yaml or use --api-key", file=sys.stderr)
        sys.exit(1)

    dispatch = {
        "exec": cmd_exec,
        "protect": cmd_protect,
        "release": cmd_release,
        "status": cmd_status,
        "list": cmd_list,
        "health": cmd_health,
        "parent": cmd_parent,
    }

    handler = dispatch.get(args.command)
    if handler:
        handler(args, cfg)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
