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
    """Evaluates policy rules from the database."""

    def __init__(self, rules_file: str = None):
        """Init from DB. If ``rules_file`` is given, may also auto-seed."""
        self._rules = []
        self._seed_file = rules_file
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
