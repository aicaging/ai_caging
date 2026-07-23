"""Background scheduler for Caging - expiration and auto-assignment."""
import time
import threading
from datetime import datetime
from typing import Optional

from . import database as db


class BackgroundScheduler:
    """Handles background tasks: expiration and auto-assignment."""

    def __init__(self, check_interval: int = 60, ttl_hours: int = 24,
                 auto_assign_after: int = 300, default_reviewer: Optional[str] = None):
        self.check_interval = check_interval
        self.ttl_hours = ttl_hours
        self.auto_assign_after = auto_assign_after
        self.default_reviewer = default_reviewer
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="caging-scheduler")
        self._thread.start()

    def stop(self):
        self._running = False

    def _loop(self):
        while self._running:
            try:
                self._check_expiration()
                self._check_auto_assign()
            except Exception:
                pass
            time.sleep(self.check_interval)

    def _check_expiration(self):
        """Expire requests that have passed their TTL."""
        now = datetime.utcnow().isoformat()
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT id FROM requests WHERE status IN ('awaiting_review','first_approved') AND expires_at < ?",
            (now,),
        ).fetchall()

        for row in rows:
            db.update_request(row["id"], status="expired")
            db._audit(row["id"], "system", "expired", "Request expired due to TTL")

    def _check_auto_assign(self):
        """Auto-assign default reviewer to unassigned requests after delay."""
        if not self.default_reviewer:
            return

        now = time.time()
        conn = db.get_connection()
        rows = conn.execute(
            "SELECT id, created_at FROM requests WHERE status = 'awaiting_review' AND reviewer_id IS NULL AND default_reviewer_used = 0"
        ).fetchall()

        for row in rows:
            try:
                created = datetime.fromisoformat(row["created_at"])
                elapsed = now - created.timestamp()
                if elapsed >= self.auto_assign_after:
                    db.update_request(
                        row["id"],
                        reviewer_id=self.default_reviewer,
                        default_reviewer_used=True,
                    )
                    db._audit(row["id"], "system", "auto_assigned",
                              f"Auto-assigned to default reviewer: {self.default_reviewer}")
            except (ValueError, TypeError):
                conn.rollback()
                pass
