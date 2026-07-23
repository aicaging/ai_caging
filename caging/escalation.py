"""Escalation module for Caging - forward requests to parent layer."""
import json
import time
import threading
from typing import Optional
import urllib.request
import urllib.error

from . import database as db


class EscalationManager:
    """Manages escalation of requests to parent Caging layer."""

    def __init__(self, base_url: str = "", api_key: str = "", timeout: int = 30,
                 poll_interval: int = 10, enabled: bool = False,
                 callback_base_url: str = ""):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.enabled = enabled
        self.callback_base_url = callback_base_url.rstrip("/")
        self._polling = False
        self._thread: Optional[threading.Thread] = None

    def escalate_request(self, request_id: str) -> bool:
        """Forward a request to parent Caging layer."""
        if not self.enabled or not self.base_url:
            return False

        req = db.get_request(request_id)
        if not req:
            return False

        # Build callback URL pointing to our own /ui/parent-callback
        # so the parent layer can notify us when the escalated request completes
        esc_callback = f"{self.callback_base_url}/ui/parent-callback" if self.callback_base_url else None

        payload = {
            "client_id": req["requester_id"],
            "command": req["payload"].get("command", ""),
            "catalog": req["payload"].get("catalog", ""),
            "timeout": req["payload"].get("timeout", 60),
            "callback_url": esc_callback,
        }

        body = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "X-API-Key": self.api_key,
        }

        try:
            request = urllib.request.Request(
                f"{self.base_url}/exec",
                data=body,
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                result = json.loads(resp.read())

            parent_id = result.get("request_id", "")
            db.update_request(
                request_id,
                status="escalated",
                escalated_to_parent=True,
                parent_request_id=parent_id,
            )
            db._audit(request_id, "system", "escalated",
                      f"Escalated to parent, parent_request_id={parent_id}")
            return True
        except Exception as e:
            db._audit(request_id, "system", "escalation_failed", str(e))
            return False

    def poll_parent_status(self, request_id: str, parent_request_id: str) -> Optional[str]:
        """Poll parent for request status update."""
        if not self.enabled:
            return None

        headers = {"X-API-Key": self.api_key}
        try:
            request = urllib.request.Request(
                f"{self.base_url}/status/{parent_request_id}",
                headers=headers,
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                result = json.loads(resp.read())
            return result.get("status")
        except Exception:
            return None

    def get_parent_request(self, parent_request_id: str) -> Optional[dict]:
        """Fetch full parent request details (with audit trail)."""
        if not self.enabled or not self.base_url:
            return None

        headers = {"X-API-Key": self.api_key}
        try:
            request = urllib.request.Request(
                f"{self.base_url}/status/{parent_request_id}",
                headers=headers,
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                result = json.loads(resp.read())
            return result
        except Exception:
            return None

    def start_polling(self):
        """Start background polling for escalated requests."""
        if self._polling or not self.enabled:
            return
        self._polling = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop_polling(self):
        self._polling = False

    def _poll_loop(self):
        while self._polling:
            try:
                escalated = db.list_requests(status="escalated")
                for req in escalated:
                    parent_id = req.get("parent_request_id")
                    if not parent_id:
                        continue
                    status = self.poll_parent_status(req["id"], parent_id)
                    if status in ("completed", "failed", "rejected"):
                        parent_full = db.get_request(req["id"])
                        if parent_full:
                            db.update_request(
                                req["id"],
                                status=status,
                                execution_result=parent_full.get("execution_result"),
                            )
                            db._audit(req["id"], "system", "parent_completed",
                                      f"Parent completed with status: {status}")
            except Exception:
                pass
            time.sleep(self.poll_interval)
