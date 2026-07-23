"""Human-in-the-loop module for Caging — approve, reject, delegate requests.

Encapsulates the review workflow logic that was previously inline in
``app.py``'s ``approve_request`` handler, making it testable and reusable.
"""

from typing import Any


class Human:
    """Human reviewer actions on a request.

    Constructor receives all external dependencies so the class
    stays decoupled from the FastAPI module and its globals.

    Dependencies
    ------------
    db : module
        Database module with ``get_request``, ``update_request``, ``_audit``.
    protecter : Protecter
        Instance used to execute ``release`` when approving a ``type==release``
        request.
    executor : Executor
        Instance used to execute commands/scripts via
        ``executor.execute_and_update(...)``.
    notify_ws : callable
        ``async (request_id, status) -> None``
    """

    def __init__(
        self,
        db: Any,
        protecter: Any,
        executor: Any,
        notify_ws: Any,
    ):
        self._db = db
        self._protecter = protecter
        self._executor = executor
        self._notify_ws = notify_ws

    # ── public actions ──────────────────────────────────────────────

    async def delegate(self, request_id: str, user: dict, body: dict) -> dict:
        """Re-assign a request to another reviewer."""
        delegate_to = body.get("delegate_to", "")
        note = body.get("note", "")

        if not delegate_to:
            raise ValueError("delegate_to required for delegation")

        self._db.update_request(request_id, reviewer_id=delegate_to)
        self._db._audit(
            request_id, user["id"], "delegated",
            f"Delegated to {delegate_to}: {note}",
        )
        await self._notify_ws(request_id, "awaiting_review")
        return {
            "request_id": request_id,
            "status": "awaiting_review",
            "delegated_to": delegate_to,
        }

    async def reject(self, request_id: str, user: dict, body: dict) -> dict:
        """Reject a request."""
        note = body.get("note", "")
        req = self._db.get_request(request_id)
        topic = req.get("topic", "na") if req else "na"

        self._db.update_request(request_id, status="rejected", review_note=note)
        self._db._audit(request_id, user["id"], "rejected",
                        f"Rejected (topic: {topic}): {note}")
        await self._notify_ws(request_id, "rejected")
        return {"request_id": request_id, "status": "rejected"}

    async def approve(self, request_id: str, user: dict, body: dict) -> dict:
        """Approve a request.

        Handles:
          - Single / dual-approval workflow
          - ``type == "exec"`` → forwarded to ``execute_and_update``
          - ``type == "release"`` → calls ``protecter.release()``
          - ``type == "protect"`` → calls ``protecter.protect()``
        """
        note = body.get("note", "")
        req = self._db.get_request(request_id)
        topic = req.get("topic", "na")
        dual = req.get("dual_approval_required", False)
        approval_count = req.get("approval_count", 0) + 1

        if dual and approval_count < 2:
            self._db.update_request(
                request_id, status="first_approved",
                approval_count=approval_count, review_note=note,
            )
            self._db._audit(request_id, user["id"], "first_approved",
                            f"First approved (topic: {topic}): {note}")
            await self._notify_ws(request_id, "first_approved")
            return {
                "request_id": request_id,
                "status": "first_approved",
                "approval_count": approval_count,
            }

        # Final approval — execute
        self._db.update_request(
            request_id, status="approved",
            approval_count=approval_count, review_note=note,
        )
        self._db._audit(request_id, user["id"], "approved",
                        f"Approved (topic: {topic}): {note}")

        req_type = req.get("type", "")

        if req_type == "exec":
            payload = req["payload"]
            return await self._executor.execute_and_update(
                request_id,
                payload.get("command", ""),
                payload.get("script_source"),
                payload.get("timeout", 60),
                payload.get("env", {}),
                force_escalate=True,
            )

        if req_type == "release":
            path = req["payload"].get("path", "")
            self._protecter.release(path, user["id"])
            self._db.update_request(request_id, status="completed")
            self._db._audit(
                request_id, "system", "released",
                f"Resource {path} released",
            )
            await self._notify_ws(request_id, "completed")
            return {
                "request_id": request_id,
                "status": "completed",
                "path": path,
            }

        if req_type == "protect":
            path = req["payload"].get("path", "")
            topic = req.get("topic", "na")
            self._protecter.protect(path, user["id"])
            self._db.update_request(request_id, status="completed")
            self._db._audit(
                request_id, "system", "protected",
                f"Resource {path} protected (topic: {topic})",
            )
            await self._notify_ws(request_id, "completed")
            return {
                "request_id": request_id,
                "status": "completed",
                "path": path,
            }

        # Unknown type — still mark completed
        self._db.update_request(request_id, status="completed")
        return {"request_id": request_id, "status": "completed"}
