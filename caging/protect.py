"""Protecter module for Caging — chattr + DB file protection with parent escalation.

Provides Protecter class that:
  - Records protection in DB (original_user, original_mode via os.stat)
  - Applies/removes chattr +i (immutable flag) on the path
  - Self-checks: if this service cannot chattr (non-root), forwards to parent service API

Root-layer services (euid=0) execute chattr directly.
Non-root services (cadmin, cage) automatically escalate to parent.
"""
import json
import os
import subprocess
import urllib.request
import urllib.error
from typing import Optional

from . import database as db


class Protecter:
    """Manage file protection (chattr +i/-i) with automatic parent escalation.

    Usage:
        protecter = Protecter(config)
        protecter.protect("/some/file", user_id="alice")
        protecter.release("/some/file", user_id="alice")

    The ``can_protect_directly()`` method checks whether this service can
    run ``chattr`` natively (only root).  When it cannot, all operations
    are forwarded to the parent Caging service configured in ``parent.*``.
    """

    def __init__(self, config: dict):
        self.config = config
        self.parent_cfg = config.get("parent", {})
        self._can_chattr: Optional[bool] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def can_protect_directly(self) -> bool:
        """Return True if this service can execute ``chattr`` natively.

        Decision is based on effective UID — only root (euid == 0) has
        permission to set/remove the immutable file attribute.
        """
        if self._can_chattr is None:
            self._can_chattr = os.geteuid() == 0
        return self._can_chattr

    def protect(self, path: str, user_id: str) -> dict:
        """Protect *path* by recording ownership in DB and applying ``chattr +i``.

        Returns the protection record (dict with *path*, *original_user*,
        *original_mode*).

        Raises
        ------
        FileNotFoundError
            If *path* does not exist.
        RuntimeError
            If ``chattr +i`` fails, or if escalation to parent fails.
        """
        # Validate path existence early
        if not os.path.exists(path):
            raise FileNotFoundError(f"Path does not exist: {path}")

        # 1. Record in DB (captures original mode via os.stat internally)
        protection = db.create_protection(path, user_id, user_id)

        # 2. Apply immutable flag (or delegate to parent)
        if self.can_protect_directly():
            try:
                self._run_chattr("+i", path)
            except Exception as exc:
                # Rollback DB record on failure
                db.delete_protection(path)
                raise RuntimeError(f"chattr +i failed: {exc}") from exc
        else:
            self._call_parent("protect", path, user_id=user_id)

        return protection

    def release(self, path: str, user_id: str) -> dict:
        """Release a previously protected *path*: remove ``chattr +i`` then
        delete the DB record.

        Returns a dict with ``{"path": ..., "status": "released"}``.

        Raises
        ------
        RuntimeError
            If *path* is not currently protected, if ``chattr -i`` fails,
            or if escalation to parent fails.
        """
        protection = db.get_protection(path)
        if not protection:
            raise RuntimeError(f"Path is not protected: {path}")

        # 1. Remove immutable flag (or delegate to parent)
        if self.can_protect_directly():
            self._run_chattr("-i", path)
        else:
            self._call_parent("release", path, user_id=user_id)

        # 2. Clean up DB record
        db.delete_protection(path)

        return {"path": path, "status": "released"}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _run_chattr(flag: str, path: str) -> None:
        """Execute ``chattr <flag> <path>``.

        Raises RuntimeError on non-zero exit.
        """
        result = subprocess.run(
            ["chattr", flag, path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise RuntimeError(f"chattr {flag} {path} failed: {stderr}")

    def _call_parent(self, action: str, path: str, user_id: str = "") -> dict:
        """Forward *action* (protect|release) to the parent Caging service.

        The request is sent as ``POST /<action>`` with the API key configured
        in ``parent.api_key``.  The *user_id* is forwarded so the parent can
        record the original requester rather than the intermediate service user.

        Raises RuntimeError if parent is not configured or the call fails.
        """
        if not self.parent_cfg.get("enabled", False):
            raise RuntimeError(
                "Cannot chattr directly (not root) and no parent configured. "
                "Set parent.enabled=true and parent.base_url in config."
            )

        base_url = self.parent_cfg["base_url"].rstrip("/")
        api_key = self.parent_cfg.get("api_key", "")
        timeout = self.parent_cfg.get("timeout", 30)

        body_dict = {"path": path}
        if user_id:
            body_dict["user_id"] = user_id
        body = json.dumps(body_dict).encode()
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": api_key,
        }

        endpoint = f"{base_url}/{action}"

        try:
            request = urllib.request.Request(
                endpoint,
                data=body,
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                result = json.loads(resp.read().decode())
            return result
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode() if exc.fp else ""
            raise RuntimeError(
                f"Parent {action} failed (HTTP {exc.code}): {error_body}"
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"Parent {action} request failed: {exc}"
            ) from exc
