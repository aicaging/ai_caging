"""Policy engine for Caging — evaluates rules from database.

Rules are stored in ``policy_rules`` table. The engine loads them ordered
by priority, evaluates conditions against request payload + env, and
returns the matching action.

A ``yaml`` file import helper is provided for migration/seed.
"""
import re
from typing import Optional

from . import database as db


class PolicyEngine:
    """Evaluates policy rules from the database.

    Supports actions: allow, require_human, escalate, deny, ai, ai_screen.
    ``ai_screen`` delegates to AIScreener which returns a decision mapped to
    allow/deny/require_human.
    """

    def __init__(self, rules_file: str = None, ai_screener=None):
        """Init from DB. If ``rules_file`` is given, may also auto-seed."""
        self._rules = []
        self._seed_file = rules_file
        self._ai_screener = ai_screener
        self.reload()

    def reload(self):
        """(Re)load rules from DB."""
        self._rules = db.list_policy_rules(enabled_only=True)
        # If DB empty and a seed file was given, auto-import once
        if not self._rules and self._seed_file:
            try:
                count = db.import_policy_rules_from_yaml(self._seed_file)
                if count > 0:
                    self._rules = db.list_policy_rules(enabled_only=True)
            except Exception:
                pass

    @property
    def rules(self) -> list:
        return self._rules

    def evaluate(
            self, payload: dict, client_id: str = "",
            system_user: str = "", request_id: str = "",
        ) -> dict:
        """Evaluate payload, resolving ``ai_screen`` via AIScreener.

        Returns a dict with the final resolved action:
            {"action": "allow"|"require_human"|"escalate"|"deny"|"ai",
             "rule_name": "...", "reason": "...", "dual_approval": bool,
             "rule_action": "..."}   # original rule action before resolution
        """
        result = self.evaluate_rules(payload, client_id, system_user)
        rule_action = result["action"]

        if rule_action == "ai_screen" and self._ai_screener:
            screen_result = self._ai_screener.screen(payload)
            decision = screen_result.get("decision", "manual")
            explanation = screen_result.get("explanation", "")
            risk_score = screen_result.get("risk_score", 50)

            # Write audit log for AI screening result
            if request_id:
                import json as _json
                try:
                    db._audit(request_id, "ai_screener", "ai_screen_result",
                              _json.dumps({"decision": decision, "explanation": explanation,
                                           "risk_score": risk_score}))
                except Exception:
                    pass

            # Map AIScreener decision to policy action
            action_map = {
                "allow": "allow",
                "deny": "deny",
                "manual": "require_human",
            }
            resolved = action_map.get(decision, "require_human")

            result.update({
                "action": resolved,
                "reason": f"AI screened: {explanation} (risk={risk_score})",
                "rule_action": rule_action,
                "ai_decision": decision,
                "ai_risk_score": risk_score,
                "ai_explanation": explanation,
            })
        elif rule_action == "ai_screen":
            # No screener configured — fall back to require_human
            result.update({
                "action": "require_human",
                "reason": "AI screening unavailable — requires human review",
                "rule_action": rule_action,
            })
        else:
            result["rule_action"] = rule_action

        return result

    def evaluate_rules(
        self, payload: dict, client_id: str = "",
        system_user: str = "",
    ) -> dict:
        """Evaluate payload against all enabled rules.

        Returns the **first** matching rule's action, plus metadata::

            {"action": "allow"|"require_human"|"escalate"|"deny"|"ai",
             "rule_name": "...", "reason": "...",
             "dual_approval": bool}
        """
        env = {
            "client_id": client_id,
            "system_user": system_user,
            "command": payload.get("command", ""),
            "base_command": payload.get("command", "").split()[0]
            if payload.get("command") else "",
            "topic": payload.get("topic", ""),
            "catalog": payload.get("catalog", ""),
            "payload": payload,
        }

        for rule in self._rules:
            condition = rule.get("condition", "true")
            if self._safe_eval(condition, env):
                return {
                    "action": rule.get("action", "require_human"),
                    "rule_name": rule.get("name", "unknown"),
                    "reason": rule.get("reason", ""),
                    "dual_approval": bool(rule.get("dual_approval", False)),
                }

        return {"action": "require_human", "rule_name": "default",
                "reason": "No matching rule", "dual_approval": False}

    def _safe_eval(self, expr: str, env: dict) -> bool:
        """Safely evaluate a Python expression string."""
        try:
            safe_globals = {
                "True": True, "False": False, "None": None,
                "true": True, "false": False,
                "re": __import__("re"),
                "str": str, "int": int, "float": float, "bool": bool,
                "len": len, "abs": abs, "any": any, "all": all,
                "min": min, "max": max, "sum": sum,
                "sorted": sorted, "list": list, "dict": dict, "set": set,
                "tuple": tuple, "isinstance": isinstance,
                "type": type, "range": range, "enumerate": enumerate,
                "zip": zip, "map": map, "filter": filter,
                "hasattr": hasattr, "getattr": getattr,
            }
            local_env = {}
            for key in env:
                if isinstance(env[key], (str, int, float, bool, list, dict, set, tuple)):
                    local_env[key] = env[key]
                else:
                    local_env[key] = str(env[key])

            code = compile(expr, "<policy>", "eval")
            result = eval(code, {"__builtins__": {}}, {**safe_globals, **local_env})
            return bool(result)
        except Exception:
            return False
