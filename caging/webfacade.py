"""WebFacade — abstraction layer between HTTP endpoints and domain classes.

Encapsulates all business logic into a single injectable class so that:
  1. FastAPI endpoints in ``app.py`` become thin HTTP-only wrappers.
  2. A CLI test (``webfacadetest.py``) can call every server function
     without an HTTP server.
"""

import json
import os
import time
from datetime import datetime
from typing import Any, Optional

import bcrypt


class WebFacade:
    """Central facade that coordinates all Caging subsystems.

    Constructor receives **every** external dependency.  No globals.
    """

    def __init__(
        self,
        db: Any,
        executor: Any,
        protecter: Any,
        human: Any,
        policy_engine: Any,
        ai_screener: Any,
        escalation_mgr: Any,
        scheduler: Any,
        rate_limiter: Any,
        ws_manager: Any,
        config: dict,
        # Auth helpers — passed as callables so they can be mocked in tests
        verify_api_key_fn: Any = None,
        create_session_token_fn: Any = None,
        verify_session_token_fn: Any = None,
        bcrypt_checkpw_fn: Any = None,
    ):
        self._db = db
        self._executor = executor
        self._protecter = protecter
        self._human = human
        self._policy_engine = policy_engine
        self._ai_screener = ai_screener
        self._escalation_mgr = escalation_mgr
        self._scheduler = scheduler
        self._rate_limiter = rate_limiter
        self._ws_manager = ws_manager
        self._config = config
        self._verify_api_key = verify_api_key_fn
        self._create_session_token = create_session_token_fn
        self._verify_session_token = verify_session_token_fn
        self._bcrypt_checkpw = bcrypt_checkpw_fn or bcrypt.checkpw

    # ── Internal helpers ────────────────────────────────────────────

    async def _notify_ws(self, request_id: str, status: str) -> None:
        """Broadcast WebSocket notification."""
        try:
            await self._ws_manager.broadcast({
                "type": "status_change",
                "request_id": request_id,
                "status": status,
                "timestamp": time.time(),
            })
        except Exception:
            pass

    def handle_parent_callback(self, body: dict) -> dict:
        """
        Handle a callback from the parent layer when an escalated request
        completes.  Look up the original escalated request by
        parent_request_id and update its status.
        """
        parent_id = body.get("request_id", "")
        status = body.get("status", "")
        execution_result = body.get("execution_result")

        if not parent_id or not status:
            raise ValueError("parent_callback: missing request_id or status")

        # Find the escalated request that has this parent_request_id
        escalated = self._db.list_requests(status="escalated", limit=500)
        match = None
        for req in escalated:
            if req.get("parent_request_id") == parent_id:
                match = req
                break

        if not match:
            raise RuntimeError(
                f"parent_callback: no escalated request found "
                f"with parent_request_id={parent_id}"
            )

        request_id = match["id"]

        self._db.update_request(
            request_id,
            status=status,
            escalated_to_parent=False,
            execution_result=execution_result,
        )
        self._db._audit(
            request_id, "parent", status,
            f"Parent completed request {parent_id} with status={status}",
        )

        return {
            "request_id": request_id,
            "status": status,
        }

    def _check_user_role(self, user: dict, allowed: list) -> None:
        """Check role; raise ValueError if not allowed."""
        from .auth import check_role
        if not check_role(user, allowed):
            raise ValueError(
                f"User role '{user.get('role')}' not in allowed roles: {allowed}"
            )

    # ── Health ──────────────────────────────────────────────────────

    def health(self) -> dict:
        """Simple health check."""
        return {
            "status": "ok",
            "service": self._config.get("service", {}).get("name", "Caging"),
        }

    # ── Exec ────────────────────────────────────────────────────────

    async def exec_command(self, body: dict, user: dict) -> dict:
        """Execute a command after policy/AI screening.

        Returns a result dict with at least ``request_id`` and ``status``.
        """
        self._check_user_role(user, ["requester", "admin"])

        client_id = body.get("client_id", user["id"])
        command = body.get("command", "")
        script_source = body.get("script_source")
        timeout = body.get("timeout", 60)
        env = body.get("env", {})
        dual_approval = body.get("dual_approval", False)
        catalog = body.get("catalog", "")
        topic = body.get("topic", "na")
        assigned_reviewer = body.get("assigned_reviewer") or None
        policy_context = body.get("policy_context", {})

        payload = {
            "command": command,
            "script_source": script_source,
            "timeout": timeout,
            "env": env,
            "catalog": catalog,
            "topic": topic,
            "policy_context": policy_context,
            "type": "exec",
        }

        req = self._db.create_request(
            req_type="exec",
            requester_id=user["id"],
            topic=topic,
            payload=payload,
            dual_approval=dual_approval,
            reviewer_id=assigned_reviewer,
            ttl_hours=self._config.get("expiration", {}).get("ttl_hours", 24),
        )

        # If topic is empty or "na", always require human review — skip policy check
        if not topic or topic.strip().lower() == "na":
            self._db.update_request(
                req["id"], status="awaiting_review",
                review_note="No topic — requires human review",
            )
            self._db._audit(req["id"], "system", "queued_for_review",
                            "No topic — always requires human review")
            await self._notify_ws(req["id"], "awaiting_review")
            return {"request_id": req["id"], "status": "awaiting_review"}

        policy_result = self._policy_engine.evaluate(
            payload, user["id"], user.get("system_user", ""), request_id=req["id"],
        )
        self._db.update_request(
            req["id"], policy_result=json.dumps(policy_result),
        )

        action = policy_result["action"]
        dual_approval = dual_approval or policy_result.get("dual_approval", False)

        if action == "deny":
            self._db.update_request(
                req["id"], status="rejected",
                review_note=policy_result["reason"],
            )
            self._db._audit(req["id"], "system", "policy_denied",
                            policy_result["reason"])
            await self._notify_ws(req["id"], "rejected")
            return {"request_id": req["id"], "status": "rejected",
                    "reason": policy_result["reason"]}

        if action == "allow":
            # If AI screened and approved, skip topic auto-approve check
            if policy_result.get("rule_action") == "ai_screen":
                return await self._executor.execute_and_update(
                    req["id"], command, script_source, timeout, env,
                )
            # Also check if topic is in the auto-approve list
            if not self._db.is_topic_auto_approved(topic):
                self._db.update_request(
                    req["id"], status="awaiting_review",
                    review_note=f"Command '{command}' is whitelisted but topic '{topic}' is not auto-approved",
                )
                self._db._audit(req["id"], "system", "queued_for_review",
                                f"Safe command, but topic '{topic}' needs manual approval")
                await self._notify_ws(req["id"], "awaiting_review")
                return {"request_id": req["id"], "status": "awaiting_review",
                        "reason": f"Topic '{topic}' not in auto-approve list"}
            return await self._executor.execute_and_update(
                req["id"], command, script_source, timeout, env,
            )

        if action == "escalate":
            if self._escalation_mgr.enabled:
                escalated = self._escalation_mgr.escalate_request(req["id"])
                if escalated:
                    # escalate_request already set status="escalated" + parent_request_id in DB
                    await self._notify_ws(req["id"], "escalated")
                    return {"request_id": req["id"], "status": "escalated",
                            "reason": policy_result["reason"]}
                else:
                    self._db.update_request(
                        req["id"], status="failed",
                        review_note=f"Escalation failed: {policy_result.get('reason', '')}",
                    )
                    self._db._audit(
                        req["id"], "system", "escalation_failed",
                        f"Escalation to parent failed for request {req['id']}",
                    )
                    await self._notify_ws(req["id"], "failed")
                    return {"request_id": req["id"], "status": "failed",
                            "reason": "Escalation failed"}
            else:
                action = "require_human"

        if action in ("require_human", "ai"):
            if action == "ai":
                ai_result = self._ai_screener.screen(payload)
                self._db.update_request(
                    req["id"],
                    risk_score=ai_result.get("risk_score", 50),
                )
                self._db._audit(req["id"], "ai", "screened",
                                json.dumps(ai_result))

                if ai_result["decision"] == "deny":
                    self._db.update_request(
                        req["id"], status="awaiting_review",
                        review_note=f"AI denied: {ai_result['explanation']}",
                    )
                    self._db._audit(req["id"], "ai", "denied",
                                    ai_result["explanation"])
                    await self._notify_ws(req["id"], "awaiting_review")
                    return {"request_id": req["id"], "status": "awaiting_review",
                            "ai_suggestion": ai_result}

                if ai_result["decision"] == "allow" and not dual_approval:
                    return await self._executor.execute_and_update(
                        req["id"], command, script_source, timeout, env,
                    )

            status = "awaiting_review"
            if dual_approval:
                status = "awaiting_review"

            self._db.update_request(req["id"], status=status)
            self._db._audit(req["id"], "system", "queued_for_review",
                            "Waiting for manual review")
            await self._notify_ws(req["id"], status)
            return {"request_id": req["id"], "status": status}

        return {"request_id": req["id"], "status": "pending"}

    # ── Protect / Release ──────────────────────────────────────────

    def protect_resource(self, body: dict, user: dict) -> dict:
        """Protect a resource — auto-approved, no human review needed."""
        self._check_user_role(user, ["requester", "admin"])
        path = body.get("path", "")
        if not path:
            raise ValueError("path is required")
        topic = body.get("topic", "na")

        payload = {"path": path, "topic": topic, "type": "protect"}
        req = self._db.create_request(
            req_type="protect",
            requester_id=user["id"],
            topic=topic,
            payload=payload,
            ttl_hours=self._config.get("expiration", {}).get("ttl_hours", 24),
        )

        # Protect is always auto-approved — but may be escalated to parent
        protection = self._protecter.protect(path, user["id"])
        if self._protecter.can_protect_directly():
            status = "completed"
            audit_action = "auto_approved"
            audit_msg = f"Topic '{topic}' auto-approved"
        else:
            status = "escalated"
            audit_action = "escalated"
            audit_msg = f"Topic '{topic}' escalated to parent for protection"
        self._db.update_request(req["id"], status=status)
        self._db._audit(req["id"], "system", audit_action, audit_msg)
        return {
            "request_id": req["id"],
            "status": status,
            "path": path,
            "original_user": user["id"],
            "original_mode": protection.get("original_mode"),
        }

    def release_resource(self, body: dict, user: dict) -> dict:
        """Release a protected resource — creates request, evaluates policy with topic."""
        self._check_user_role(user, ["requester", "admin"])
        path = body.get("path", "")
        if not path:
            raise ValueError("path is required")
        topic = body.get("topic", "na")
        reason = body.get("reason", "")

        payload = {"path": path, "topic": topic, "reason": reason, "type": "release"}
        req = self._db.create_request(
            req_type="release",
            requester_id=user["id"],
            topic=topic,
            payload=payload,
            ttl_hours=self._config.get("expiration", {}).get("ttl_hours", 24),
        )

        # If topic is empty or "na", always require human review — skip policy check
        if not topic or topic.strip().lower() == "na":
            self._db.update_request(req["id"], status="awaiting_review",
                                    review_note="No topic — requires human review")
            self._db._audit(req["id"], "system", "queued_for_review",
                            "No topic — always requires human review")
            return {"request_id": req["id"], "status": "awaiting_review"}

        policy_result = self._policy_engine.evaluate(
            payload, user["id"], user.get("system_user", ""), request_id=req["id"],
        )
        self._db.update_request(
            req["id"], policy_result=json.dumps(policy_result),
        )

        action = policy_result["action"]
        if action == "deny":
            self._db.update_request(req["id"], status="rejected",
                                    review_note=policy_result["reason"])
            self._db._audit(req["id"], "system", "policy_denied",
                            policy_result["reason"])
            return {"request_id": req["id"], "status": "rejected",
                    "reason": policy_result["reason"]}

        if action == "allow":
            self._protecter.release(path, user["id"])
            self._db.update_request(req["id"], status="completed")
            self._db._audit(req["id"], "system", "auto_approved",
                            f"Topic '{topic}' auto-approved")
            return {
                "request_id": req["id"],
                "status": "ok",
                "path": path,
                "released_by": user["id"],
            }

        # require_human — queue for review
        self._db.update_request(req["id"], status="awaiting_review")
        self._db._audit(req["id"], "system", "queued_for_review",
                        f"Topic '{topic}' requires human review")
        return {"request_id": req["id"], "status": "awaiting_review"}

    # ── Approve / Reject / Delegate ────────────────────────────────

    async def approve_request(
        self, request_id: str, body: dict, user: dict,
    ) -> dict:
        """Approve, reject, or delegate a request."""
        self._check_user_role(user, ["reviewer", "admin"])

        req = self._db.get_request(request_id)
        if not req:
            raise ValueError("Request not found")

        if user["role"] == "admin" and req["requester_id"] == user["id"]:
            raise ValueError("Admin cannot approve own requests")

        if req["status"] not in ("awaiting_review", "first_approved"):
            raise ValueError(
                f"Cannot approve request in status: {req['status']}",
            )

        action = body.get("status", "")

        if action == "delegate":
            return await self._human.delegate(request_id, user, body)
        elif action == "rejected":
            return await self._human.reject(request_id, user, body)
        elif action == "approved":
            # Re-evaluate policy with topic for auto-approve
            topic = req.get("topic", "na")
            payload = dict(req.get("payload", {}))
            payload["topic"] = topic
            policy_result = self._policy_engine.evaluate(
                payload, user["id"], user.get("system_user", ""), request_id=request_id,
            )
            if policy_result["action"] == "allow":
                self._db._audit(request_id, "system", "topic_auto_approved",
                                f"Topic '{topic}' matched auto-approve")
            return await self._human.approve(request_id, user, body)
        else:
            raise ValueError(f"Invalid status: {action}")

    async def approve_all_awaiting(self, user: dict) -> dict:
        """Approve all requests currently awaiting review."""
        self._check_user_role(user, ["reviewer", "admin"])

        requests = self._db.list_requests(status="awaiting_review", limit=500)
        reqs = requests if isinstance(requests, list) else requests.get("requests", [])

        approved = 0
        errors = []
        for req in reqs:
            try:
                await self._human.approve(req["id"], user, {"status": "approved"})
                approved += 1
            except Exception as e:
                errors.append({"request_id": req["id"], "error": str(e)})

        return {"approved": approved, "errors": errors, "total": len(reqs)}

    async def delete_all_requests(self, user: dict) -> dict:
        """Delete all requests from the database."""
        self._check_user_role(user, ["admin"])
        count = self._db.delete_all_requests()
        return {"deleted": count, "message": f"Deleted {count} requests"}

    # ── Query ───────────────────────────────────────────────────────

    def list_requests(
        self, status: Optional[str] = None, limit: int = 100,
        user: Optional[dict] = None,
    ) -> dict:
        """List requests with optional status filter."""
        reqs = self._db.list_requests(status=status, limit=limit)
        return {"requests": reqs, "count": len(reqs)}

    def get_status(self, request_id: str, user: Optional[dict] = None) -> dict:
        """Get full status with audit trail."""
        req = self._db.get_request(request_id)
        if not req:
            raise ValueError("Request not found")
        audit = self._db.get_audit_log(request_id)
        req["audit_trail"] = audit

        # ── For escalated requests, query parent for live status ──
        parent_id = req.get("parent_request_id")
        if parent_id and req.get("status") == "escalated":
            live_status = self._escalation_mgr.poll_parent_status(request_id, parent_id)
            if live_status:
                req["parent_status"] = live_status
                # If parent completed, pull full parent request details
                if live_status in ("completed", "failed", "rejected"):
                    parent_req = self._escalation_mgr.get_parent_request(parent_id)
                    if parent_req and parent_req.get("execution_result"):
                        req["execution_result"] = parent_req["execution_result"]

        # ── Also pull execution_result from parent if local is missing ──
        if parent_id and not req.get("execution_result"):
            parent_req = self._escalation_mgr.get_parent_request(parent_id)
            if parent_req and parent_req.get("execution_result"):
                req["execution_result"] = parent_req["execution_result"]
                if not req.get("parent_status"):
                    req["parent_status"] = parent_req.get("status")

        return req

    def get_user(self, client_id: str) -> Optional[dict]:
        """Get a user by ID."""
        return self._db.get_user(client_id)

    # ── Admin ───────────────────────────────────────────────────────

    def reload_policy(self, user: dict) -> dict:
        """Reload policy rules from DB."""
        self._check_user_role(user, ["admin"])
        self._policy_engine.reload()
        return {"status": "ok", "message": "Policy reloaded"}

    def list_policy_rules(self, user: dict) -> dict:
        """List all policy rules."""
        self._check_user_role(user, ["admin"])
        rules = self._db.list_policy_rules()
        return {"rules": rules, "count": len(rules)}

    def add_policy_rule(self, user: dict, body: dict) -> dict:
        """Create a new policy rule."""
        self._check_user_role(user, ["admin"])
        rule = self._db.create_policy_rule(
            name=body.get("name", ""),
            condition=body.get("condition", "true"),
            action=body.get("action", "require_human"),
            reason=body.get("reason", ""),
            dual_approval=body.get("dual_approval", False),
            priority=body.get("priority", 100),
            enabled=body.get("enabled", True),
        )
        return {"rule": rule, "status": "ok"}

    def update_policy_rule(self, user: dict, rule_id: int, body: dict) -> dict:
        """Update an existing policy rule."""
        self._check_user_role(user, ["admin"])
        rule = self._db.update_policy_rule(
            rule_id,
            name=body.get("name"),
            condition=body.get("condition"),
            action=body.get("action"),
            reason=body.get("reason"),
            dual_approval=body.get("dual_approval"),
            priority=body.get("priority"),
            enabled=body.get("enabled"),
        )
        if not rule:
            raise ValueError(f"Policy rule {rule_id} not found")
        return {"rule": rule, "status": "ok"}

    def delete_policy_rule(self, user: dict, rule_id: int) -> dict:
        """Delete a policy rule."""
        self._check_user_role(user, ["admin"])
        ok = self._db.delete_policy_rule(rule_id)
        if not ok:
            raise ValueError(f"Policy rule {rule_id} not found")
        return {"status": "ok", "message": f"Rule {rule_id} deleted"}

    def import_policy_yaml(self, user: dict, body: dict) -> dict:
        """Import rules from a YAML file path."""
        self._check_user_role(user, ["admin"])
        yaml_path = body.get("path", "")
        if not yaml_path or not os.path.isfile(yaml_path):
            raise ValueError(f"File not found: {yaml_path}")
        count = self._db.import_policy_rules_from_yaml(yaml_path)
        self._policy_engine.reload()
        return {"status": "ok", "imported": count}

    # ── Auth / Login ────────────────────────────────────────────────

    def login(self, body: dict) -> dict:
        """Authenticate user and return session data with DB-persisted session."""
        username = body.get("username", "")
        password = body.get("password", "")

        conn = self._db.get_connection()
        rows = conn.execute(
            "SELECT * FROM users WHERE id = ?", (username,),
        ).fetchall()
        for row in rows:
            user = dict(row)
            # Try password_hash first (normal password login)
            pw_hash = user.get("password_hash")
            if pw_hash and self._bcrypt_checkpw(
                password.encode(), pw_hash.encode(),
            ):
                token = self._create_session_token(user["id"])
                return {
                    "status": "ok",
                    "client_id": user["id"],
                    "role": user["role"],
                    "session_token": token,
                }
            # Fallback: if no password_hash set, allow login with API key
            api_key_hash = user.get("api_key_hash")
            if not pw_hash and api_key_hash and self._bcrypt_checkpw(
                password.encode(), api_key_hash.encode(),
            ):
                token = self._create_session_token(user["id"])
                return {
                    "status": "ok",
                    "client_id": user["id"],
                    "role": user["role"],
                    "session_token": token,
                }

        raise ValueError("Invalid username or password")

    # ── Web UI data helpers ─────────────────────────────────────────

    def _verify_ui_session(self, session_token: str) -> str:
        """Verify a UI session token: HMAC check + DB persistence check.
        Returns client_id if valid, raises ValueError otherwise."""
        from .auth import verify_session_token
        client_id = verify_session_token(session_token)
        if not client_id:
            raise ValueError("Not authenticated")
        return client_id

    def list_requests_for_ui(
        self, session_token: str, status: Optional[str] = None,
    ) -> dict:
        """List requests formatted for Web UI consumption."""
        client_id = self._verify_ui_session(session_token)

        user = self._db.get_user(client_id)
        if not user:
            raise ValueError("User not found")

        if user["role"] in ("reviewer", "admin"):
            requests = self._db.list_requests(status=status)
        else:
            requests = self._db.list_requests()

        for r in requests:
            if isinstance(r.get("payload"), str):
                try:
                    r["payload"] = json.loads(r["payload"])
                except json.JSONDecodeError:
                    pass
        return {"requests": requests, "client_id": client_id}

    def ai_risk_analysis(
        self, request_id: str, user: dict,
    ) -> dict:
        """Run AI risk analysis on a pending request's command."""
        # Verify user has reviewer or admin role
        db_user = self._db.get_user(user["id"])
        if not db_user:
            raise ValueError("User not found")
        if db_user["role"] not in ("reviewer", "admin"):
            raise ValueError("Insufficient permissions")

        req = self._db.get_request(request_id)
        if not req:
            raise ValueError("Request not found")

        payload = req.get("payload", {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                pass

        # Build a context-rich payload for AI analysis
        # Include extra context about who's requesting and request metadata
        analysis_payload = {
            **payload,
            "_meta": {
                "requester_id": req.get("requester_id", ""),
                "request_id": request_id,
                "type": req.get("type", ""),
            }
        }

        result = self._ai_screener.screen(analysis_payload)
        return {
            "request_id": request_id,
            "risk_score": result.get("risk_score", 50),
            "decision": result.get("decision", "manual"),
            "explanation": result.get("explanation", "No analysis available"),
        }

    def get_request_detail(
        self, session_token: str, request_id: str,
    ) -> dict:
        """Get request detail for UI."""
        client_id = self._verify_ui_session(session_token)

        req = self._db.get_request(request_id)
        if not req:
            raise ValueError("Not found")

        # Parse JSON payload for UI consumption
        if isinstance(req.get("payload"), str):
            try:
                req["payload"] = json.loads(req["payload"])
            except json.JSONDecodeError:
                pass

        audit = self._db.get_audit_log(request_id)
        return {"req": req, "audit": audit, "client_id": client_id}

    def get_policy_for_request(
        self, session_token: str, request_id: str,
    ) -> dict:
        """Get the policy rule matching a given request, plus all rules for UI.

        Returns matched_rule, all_rules, and request_info so the frontend
        can populate a policy edit/create dialog.
        """
        client_id = self._verify_ui_session(session_token)

        req = self._db.get_request(request_id)
        if not req:
            raise ValueError("Request not found")

        # Parse payload
        payload = req.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}

        # Evaluate policy to find matching rule (raw rules only — no AI screening)
        user = self._db.get_user(client_id)
        policy_result = self._policy_engine.evaluate_rules(
            payload, client_id, user.get("system_user", ""),
        )

        # Gather all rules (for the dialog dropdown)
        all_rules = self._db.list_policy_rules()

        # Build request_info for pre-filling form
        request_info = {
            "request_id": request_id,
            "command": payload.get("command", "") if isinstance(payload, dict) else "",
            "topic": payload.get("topic", "") if isinstance(payload, dict) else "",
            "base_command": (payload.get("command", "").split()[0]
                             if isinstance(payload, dict) and payload.get("command") else ""),
        }

        return {
            "matched_rule": policy_result,
            "all_rules": all_rules,
            "request_info": request_info,
        }

    # ── Auto-Approve Topic management ─────────────────────────────────

    def list_auto_approve_topics(self, session_token: str) -> dict:
        """List all auto-approved topics (admin only)."""
        client_id = self._verify_ui_session(session_token)
        user = self._db.get_user(client_id)
        if user["role"] not in ("reviewer", "admin"):
            raise ValueError("Insufficient permissions")
        topics = self._db.list_auto_approve_topics()
        return {"topics": topics}

    def add_auto_approve_topic(self, session_token: str, topic: str) -> dict:
        """Add a topic to the auto-approve list."""
        client_id = self._verify_ui_session(session_token)
        user = self._db.get_user(client_id)
        if user["role"] not in ("reviewer", "admin"):
            raise ValueError("Insufficient permissions")
        result = self._db.add_auto_approve_topic(topic, client_id)
        if result is None:
            raise ValueError(f"Topic '{topic}' already in auto-approve list")
        self._db._audit("SYSTEM", client_id, "auto_approve_topic_added",
                        f"Topic '{topic}' added to auto-approve list")
        return result

    def remove_auto_approve_topic(self, session_token: str, topic: str) -> bool:
        """Remove a topic from the auto-approve list."""
        client_id = self._verify_ui_session(session_token)
        user = self._db.get_user(client_id)
        if user["role"] not in ("reviewer", "admin"):
            raise ValueError("Insufficient permissions")
        removed = self._db.remove_auto_approve_topic(topic)
        if removed:
            self._db._audit("SYSTEM", client_id, "auto_approve_topic_removed",
                            f"Topic '{topic}' removed from auto-approve list")
        return removed

    async def approve_topic_from_request(self, session_token: str, request_id: str) -> dict:
        """From a request detail page: add the request's topic to auto-approve list,
        then approve and execute the request."""
        client_id = self._verify_ui_session(session_token)
        user = self._db.get_user(client_id)
        return await self._approve_topic_from_request(user, request_id)

    async def _approve_topic_from_request(self, user: dict, request_id: str) -> dict:
        """Internal: approve request and add topic to auto-approve list, given a user dict."""
        if user["role"] not in ("reviewer", "admin"):
            raise ValueError("Insufficient permissions")

        req = self._db.get_request(request_id)
        if not req:
            raise ValueError("Request not found")
        if req["status"] != "awaiting_review":
            raise ValueError(f"Cannot approve topic from request in status '{req['status']}'")

        # Extract topic from request
        payload = req.get("payload")
        if isinstance(payload, str):
            payload = json.loads(payload)
        topic = payload.get("topic", "") if isinstance(payload, dict) else req.get("topic", "")
        if not topic or topic.strip().lower() == "na":
            raise ValueError("Request has no topic — cannot auto-approve")

        # Add topic to auto-approve list if not already
        try:
            self._db.add_auto_approve_topic(topic, client_id)
        except Exception:
            pass  # already exists, that's fine

        self._db._audit(request_id, client_id, "topic_auto_approved",
                        f"Topic '{topic}' added to auto-approve list and request approved")

        # Approve the request
        self._db.update_request(
            request_id, status="approved", reviewer_id=client_id,
            review_note=f"Topic '{topic}' auto-approved",
        )
        self._db._audit(request_id, client_id, "approved",
                        f"Approved after topic '{topic}' auto-approval")

        # Execute if it's an exec-type request
        if req["type"] == "exec":
            command = payload.get("command", "") if isinstance(payload, dict) else ""
            script_source = payload.get("script_source", "cli") if isinstance(payload, dict) else "cli"
            timeout = payload.get("timeout", 30) if isinstance(payload, dict) else 30
            env = payload.get("env", {}) if isinstance(payload, dict) else {}
            result = await self._executor.execute_and_update(
                request_id, command, script_source, timeout, env,
            )
            return {"status": "executed", "topic": topic, "result": result}

        return {"status": "approved", "topic": topic}

    # ── Chat (moved to ai_chat.py + chat.js) ─────────────────────────
    # Built-in commands (? / >) are now handled client-side in chat.js.
    # Pure AI chat is handled by ai_chat.py via the /chat endpoint.
