"""Command execution module for Caging — Executor class.

Encapsulates:
  - Command/script execution (subprocess)
  - Pre-flight permission checks
  - DB lifecycle + auto-escalation on failure
"""

import subprocess
import tempfile
import os
import signal
import shlex
from datetime import datetime
from typing import Any, Callable, Coroutine, Optional


class Executor:
    """Execute commands/scripts with pre-flight checks and DB lifecycle.

    Constructor receives all external dependencies as callables so the class
    stays decoupled from the FastAPI module.

    Parameters
    ----------
    db : module
        Database module with ``get_request``, ``update_request``, ``_audit``.
    escalation_mgr : EscalationManager
        Used to escalate on permission-denied failures.
    notify_ws : callable
        ``async (request_id, status) -> None``
    """

    def __init__(
        self,
        db: Any,
        escalation_mgr: Any,
        notify_ws: Callable[..., Coroutine[Any, Any, None]],
        config: Optional[dict] = None,
    ):
        self._db = db
        self._escalation_mgr = escalation_mgr
        self._notify_ws = notify_ws
        self._config = config or {}

    # ── public API ─────────────────────────────────────────────────

    async def execute_and_update(
        self,
        request_id: str,
        command: str,
        script_source: Optional[str],
        timeout: int,
        env: dict,
        force_escalate: bool = False,
    ) -> dict:
        """Execute a command or script and update the request DB record.

        Includes:
          - (B) Pre-flight permission check → escalate on failure
          - Actual execution
          - (A) Post-execution auto-escalate on Permission denied

        Parameters
        ----------
        force_escalate : bool
            If True, bypass ``escalation_mgr.enabled`` and always escalate
            on permission failure. Used when the request has already been
            approved by a human reviewer.
        """
        # === C: Config substitution ({{key}} → config value) ===
        if command and "{{" in command:
            substituted, missing = self._substitute_placeholders(command)
            if missing:
                self._db._audit(
                    request_id, "system", "substitution_failed",
                    f"Missing config keys: {', '.join(missing)}",
                )
                if self._escalation_mgr.enabled or force_escalate:
                    escalated = self._escalation_mgr.escalate_request(request_id)
                    if escalated:
                        await self._notify_ws(request_id, "escalated")
                        return {
                            "request_id": request_id,
                            "status": "escalated",
                            "reason": f"Missing config keys: {', '.join(missing)}",
                        }
                self._db.update_request(
                    request_id, status="failed",
                    review_note=f"Missing config keys: {', '.join(missing)}",
                )
                await self._notify_ws(request_id, "failed")
                return {
                    "request_id": request_id,
                    "status": "failed",
                    "reason": f"Escalation failed: missing config keys: {', '.join(missing)}",
                }
            command = substituted

        # === B: Pre-flight permission check ===
        if command and not script_source:
            perm_check = self.precheck_command_permissions(command)
            if not perm_check["ok"] and (self._escalation_mgr.enabled or force_escalate):
                self._db._audit(
                    request_id, "system", "precheck_failed",
                    perm_check["reason"],
                )
                escalated = self._escalation_mgr.escalate_request(request_id)
                if escalated:
                    # escalate_request already set status="escalated" + parent_request_id in DB
                    await self._notify_ws(request_id, "escalated")
                    return {
                        "request_id": request_id,
                        "status": "escalated",
                        "reason": perm_check["reason"],
                    }
                else:
                    self._db.update_request(
                        request_id, status="failed",
                        review_note=perm_check["reason"],
                    )
                    await self._notify_ws(request_id, "failed")
                    return {
                        "request_id": request_id,
                        "status": "failed",
                        "reason": f"Escalation failed: {perm_check['reason']}",
                    }

        self._db.update_request(request_id, status="executing")
        self._db._audit(request_id, "system", "executing", "Starting execution")

        if script_source:
            result = self.execute_script(script_source, timeout, env)
        else:
            result = self.execute_command(command, timeout, env)

        final_status = "completed" if result["returncode"] == 0 else "failed"

        # === A: Post-execution auto-escalate on Permission denied ===
        if final_status == "failed" and (self._escalation_mgr.enabled or force_escalate):
            stderr_lower = (result.get("stderr") or "").lower()
            if (
                "permission denied" in stderr_lower
                or "eacces" in stderr_lower
                or "not permitted" in stderr_lower
            ):
                self._db._audit(
                    request_id, "system", "auto_escalate_attempt",
                    "Permission denied → attempting escalation to parent",
                )
                escalated = self._escalation_mgr.escalate_request(request_id)
                if escalated:
                    # escalate_request already set status="escalated" + parent_request_id in DB
                    await self._notify_ws(request_id, "escalated")
                    return {
                        "request_id": request_id,
                        "status": "escalated",
                        "reason": "Permission denied — escalated to parent layer",
                    }
                else:
                    self._db.update_request(
                        request_id, status="failed",
                        execution_result=result,
                        review_note="Auto-escalate failed",
                    )
                    self._db._audit(
                        request_id, "system", "escalation_failed",
                        "Permission denied but escalation to parent failed",
                    )
                    await self._notify_ws(request_id, "failed")
                    return {
                        "request_id": request_id,
                        "status": "failed",
                        "reason": "Permission denied — escalation failed",
                        "execution_result": result,
                    }

        self._db.update_request(
            request_id, status=final_status,
            execution_result=result,
            executed_at=datetime.utcnow().isoformat(),
        )
        self._db._audit(
            request_id, "system", final_status,
            f"Return code: {result['returncode']}",
        )

        await self._notify_ws(request_id, final_status)

        return {"request_id": request_id, "status": final_status, "result": result}

    # ── configuration substitution ─────────────────────────────────

    def _substitute_placeholders(self, command: str) -> tuple[str, list[str]]:
        """Replace ``{{key}}`` or ``{{section.key}}`` with values from config.

        Returns
        -------
        (substituted_command, missing_keys)
            ``missing_keys`` is empty when all placeholders were resolved.
        """
        import re

        missing: list[str] = []

        def _resolve(key: str):
            parts = key.strip().split(".")
            val: Any = self._config
            for p in parts:
                if isinstance(val, dict):
                    val = val.get(p)
                else:
                    return None
            return val

        def _replace(m: re.Match) -> str:
            raw = m.group(1).strip()
            resolved = _resolve(raw)
            if resolved is None:
                missing.append(raw)
                return m.group(0)  # leave placeholder intact
            return str(resolved)

        substituted = re.sub(r"\{\{(.+?)\}\}", _replace, command)
        return substituted, missing

    # ── execution primitives ────────────────────────────────────────

    @staticmethod
    def precheck_command_permissions(command: str) -> dict:
        """Pre-flight permission check (B).

        Extract file paths from command args and verify current user has access.
        Returns dict: ok(bool), inaccessible(list of paths), reason(str).
        """
        try:
            args = shlex.split(command)
        except Exception:
            args = command.split()

        if not args:
            return {"ok": True, "inaccessible": [], "reason": ""}

        cmd = args[0].lower()
        # Commands that write to the target path
        write_cmds = {
            "cp", "mv", "rm", "dd", "touch", "mkdir", "rmdir", "tee",
            "sed", "awk", "chmod", "chown", "chattr", "truncate",
            "install", "ln", "mknod", "mkfifo",
        }
        need_write = cmd in write_cmds
        inaccessible = []

        for arg in args[1:]:
            # Skip flags, options, env vars, redirects
            if arg.startswith("-") or arg.startswith("$") or arg in (">", "<", ">>", "|"):
                continue

            # Expand ~
            path = os.path.expanduser(arg) if arg.startswith("~") else arg

            # Only check absolute paths and explicit relative paths
            if not (path.startswith("/") or path.startswith("./")):
                continue

            if os.path.exists(path):
                mode = os.W_OK if need_write else os.R_OK
                if not os.access(path, mode):
                    inaccessible.append(path)
            elif need_write:
                # File doesn't exist yet — check parent directory write permission
                parent = os.path.dirname(path)
                if parent and os.path.exists(parent) and not os.access(parent, os.W_OK):
                    inaccessible.append(path)

        reason = ""
        if inaccessible:
            action = "write to" if need_write else "read"
            reason = f"Current user lacks permission to {action}: {', '.join(inaccessible)}"

        return {
            "ok": len(inaccessible) == 0,
            "inaccessible": inaccessible,
            "need_write": need_write,
            "reason": reason,
        }

    @staticmethod
    def execute_command(
        command: str,
        timeout: int = 60,
        env: Optional[dict] = None,
    ) -> dict:
        """Execute a command string with shell=False, args parsed from command.

        Returns dict with stdout, stderr, returncode, timed_out.
        """
        try:
            args = shlex.split(command)
        except Exception:
            args = command.split()

        try:
            proc_env = os.environ.copy()
            if env:
                proc_env.update(env)

            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=proc_env,
            )
            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
                "timed_out": False,
            }
        except subprocess.TimeoutExpired:
            return {
                "stdout": "",
                "stderr": f"Command timed out after {timeout}s",
                "returncode": -1,
                "timed_out": True,
            }
        except FileNotFoundError as e:
            return {"stdout": "", "stderr": str(e), "returncode": -1, "timed_out": False}
        except Exception as e:
            return {"stdout": "", "stderr": str(e), "returncode": -1, "timed_out": False}

    @staticmethod
    def execute_script(
        script_source: str,
        timeout: int = 60,
        env: Optional[dict] = None,
    ) -> dict:
        """Write script to temp file and execute with /bin/bash."""
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".sh", delete=False, prefix="caging_"
            ) as f:
                f.write(script_source)
                tmp_path = f.name
            os.chmod(tmp_path, 0o700)

            proc_env = os.environ.copy()
            if env:
                proc_env.update(env)

            result = subprocess.run(
                ["/bin/bash", tmp_path],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=proc_env,
            )
            return {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
                "timed_out": False,
            }
        except subprocess.TimeoutExpired:
            return {
                "stdout": "",
                "stderr": f"Script timed out after {timeout}s",
                "returncode": -1,
                "timed_out": True,
            }
        except Exception as e:
            return {"stdout": "", "stderr": str(e), "returncode": -1, "timed_out": False}
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
