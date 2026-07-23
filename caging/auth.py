"""Authentication module for Caging - API key validation and role enforcement."""
import bcrypt
import re
import secrets
from typing import Optional

API_KEY_PREFIX = "ak_live_"


def generate_api_key() -> tuple:
    """Generate a new API key and its bcrypt hash.
    Returns (plain_key, hashed_key)."""
    plain = API_KEY_PREFIX + secrets.token_hex(32)
    hashed = bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()
    return plain, hashed


def verify_api_key(plain_key: str, stored_hash: str) -> bool:
    """Verify a plain API key against its stored bcrypt hash."""
    return bcrypt.checkpw(plain_key.encode(), stored_hash.encode())


def extract_client_id_from_key(plain_key: str) -> Optional[str]:
    """Extract client_id from the first part of the key material.
    Not used for security - only for display/hint purposes."""
    if plain_key.startswith(API_KEY_PREFIX):
        return None  # purely random
    return None


# ---- Rate Limiter (in-memory sliding window) ----
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, per_minute: int = 60, per_hour: int = 1000):
        self.per_minute = per_minute
        self.per_hour = per_hour
        self._windows: dict[str, deque] = defaultdict(deque)

    def check(self, client_id: str) -> tuple:
        """Check if request is allowed. Returns (allowed, reason)."""
        now = time.time()
        window = self._windows[client_id]

        # Remove old entries
        while window and window[0] < now - 3600:
            window.popleft()

        if len(window) >= self.per_hour:
            return False, "hourly rate limit exceeded"

        # Count last minute
        cutoff = now - 60
        minute_count = sum(1 for t in window if t >= cutoff)
        if minute_count >= self.per_minute:
            return False, "minute rate limit exceeded"

        window.append(now)
        return True, "ok"


# ---- Role verification ----
def check_role(user: dict, allowed_roles: list) -> bool:
    """Check if user's role is in allowed_roles."""
    return user.get("role") in allowed_roles


# ---- Session management for Web UI ----
import hashlib
import hmac

SESSION_SECRET = None


def init_session_secret(secret: str):
    global SESSION_SECRET
    SESSION_SECRET = secret


def create_session_token(client_id: str) -> str:
    """Create a simple HMAC-based session token."""
    msg = f"{client_id}:"
    sig = hmac.new(SESSION_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{client_id}:{sig}"


def verify_session_token(token: str) -> Optional[str]:
    """Verify session token and return client_id."""
    try:
        parts = token.split(":")
        if len(parts) != 2:
            return None
        client_id, sig = parts
        expected = hmac.new(SESSION_SECRET.encode(), f"{client_id}:".encode(), hashlib.sha256).hexdigest()[:16]
        if hmac.compare_digest(sig, expected):
            return client_id
    except Exception:
        pass
    return None
