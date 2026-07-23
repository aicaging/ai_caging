"""Main FastAPI application for Caging service — thin HTTP wrappers over WebFacade."""

import json
import logging
import os
import traceback
from contextlib import asynccontextmanager
from typing import Optional, Any

logger = logging.getLogger(__name__)

import bcrypt
import yaml
from fastapi import FastAPI, HTTPException, Request, Depends, Header, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import database as db
from .auth import (
    generate_api_key,
    verify_api_key,
    check_role,
    RateLimiter,
    init_session_secret,
    create_session_token,
    verify_session_token,
)
from .policy_engine import PolicyEngine
from .ai_screener import AIScreener
from .executor import Executor
from .websocket_manager import ws_manager
from .escalation import EscalationManager
from .scheduler import BackgroundScheduler
from .protect import Protecter
from .human import Human
from .webfacade import WebFacade
from .ai_chat import AIChat

# ── Globals ────────────────────────────────────────────────────────
CONFIG = {}
rate_limiter = None
webfacade = None
ai_chat = None

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── Helper functions used by endpoint wrappers ────────────────────
def _get_session_token(request: Request) -> str:
    """Extract session token from cadmin-session-id cookie."""
    return request.cookies.get("cadmin-session-id", "")

async def _notify_ws(request_id: str, status: str):
    try:
        await ws_manager.broadcast({
            "type": "status_change",
            "request_id": request_id,
            "status": status,
            "timestamp": __import__("time").time(),
        })
    except Exception:
        pass


# ── Init ───────────────────────────────────────────────────────────
def load_config(config_path: str = "config.yaml"):
    global CONFIG, rate_limiter, webfacade, ai_chat
    with open(config_path) as f:
        CONFIG = yaml.safe_load(f)

    svc = CONFIG.get("service", {})
    auth_cfg = CONFIG.get("auth", {})
    ai_cfg = CONFIG.get("ai", {})
    parent_cfg = CONFIG.get("parent", {})
    notif_cfg = CONFIG.get("notifications", {})
    review_cfg = CONFIG.get("review", {})
    exp_cfg = CONFIG.get("expiration", {})

    init_session_secret(auth_cfg.get("session_secret", os.environ.get("CAGING_SESSION_SECRET", "change-me")))

    rl_cfg = auth_cfg.get("rate_limit", {})
    rate_limiter = RateLimiter(
        per_minute=rl_cfg.get("per_minute", 60),
        per_hour=rl_cfg.get("per_hour", 1000),
    )
    globals()["rate_limiter"] = rate_limiter

    policy_rules_file = os.environ.get("CAGING_POLICY") or os.path.join(
        os.path.dirname(config_path),
        CONFIG.get("policy", {}).get("rules_file", "policy.yaml"),
    )
    policy_engine = PolicyEngine(rules_file=policy_rules_file)

    ai_screener = AIScreener(
        provider=ai_cfg.get("provider", "openai"),
        api_key=ai_cfg.get("api_key", ""),
        model=ai_cfg.get("model", "gpt-4"),
        fallback=ai_cfg.get("fallback", "manual"),
        base_url=ai_cfg.get("base_url", ""),
    )

    # Build the service's own callback base URL for parent escalation callbacks
    svc_port = svc.get("port", 8000)
    callback_base_url = svc.get("callback_base_url", f"http://localhost:{svc_port}")

    escalation_mgr = EscalationManager(
        base_url=parent_cfg.get("base_url", ""),
        api_key=parent_cfg.get("api_key", ""),
        timeout=parent_cfg.get("timeout", 30),
        poll_interval=parent_cfg.get("poll_interval_seconds", 10),
        enabled=parent_cfg.get("enabled", False),
        callback_base_url=callback_base_url,
    )

    protecter = Protecter(CONFIG)

    executor = Executor(
        db=db,
        escalation_mgr=escalation_mgr,
        notify_ws=_notify_ws,
        config=CONFIG,
    )

    human = Human(
        db=db,
        protecter=protecter,
        executor=executor,
        notify_ws=_notify_ws,
    )

    scheduler = BackgroundScheduler(
        check_interval=exp_cfg.get("check_interval_seconds", 60),
        ttl_hours=exp_cfg.get("ttl_hours", 24),
        auto_assign_after=review_cfg.get("auto_assign_after_seconds", 300),
        default_reviewer=review_cfg.get("default_reviewer"),
    )

    # ── Build WebFacade with all deps ────────────────────────
    webfacade = WebFacade(
        db=db,
        executor=executor,
        protecter=protecter,
        human=human,
        policy_engine=policy_engine,
        ai_screener=ai_screener,
        escalation_mgr=escalation_mgr,
        scheduler=scheduler,
        rate_limiter=rate_limiter,
        ws_manager=ws_manager,
        config=CONFIG,
        create_session_token_fn=create_session_token,
        verify_session_token_fn=verify_session_token,
        verify_api_key_fn=verify_api_key,
    )

    # ── Build AIChat (pure AI chat, no built-in commands) ────
    ai_chat = AIChat(ai_screener)


_config_path = os.environ.get("CAGING_CONFIG") or os.path.join(BASE_DIR, "config.yaml")
load_config(_config_path)


# ── Auth dependencies ──────────────────────────────────────────────
async def verify_session_dependency(request: Request):
    """Verify UI session via Authorization Bearer header (primary) or ?token= query param (page load).
    For UI-only endpoints. Raises 401 if invalid/expired."""
    session_token = _get_session_token(request)
    if not session_token:
        raise HTTPException(status_code=401, detail="Missing session token")
    # Verify HMAC signature
    client_id = verify_session_token(session_token)
    if not client_id:
        raise HTTPException(status_code=401, detail="Invalid session token")
    user = webfacade.get_user(client_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    request.state.user = user
    return user


async def verify_api_key_dependency(
    request: Request,
    x_api_key: Optional[str] = Header(None),
):
    """Verify API key from X-API-Key header only.
    For requester/API endpoints. No session fallback."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")

    allowed, _ = rate_limiter.check("global")
    if not allowed:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    from .database import get_connection
    conn = get_connection()
    rows = conn.execute("SELECT * FROM users").fetchall()
    for row in rows:
        user = dict(row)
        if verify_api_key(x_api_key, user["api_key_hash"]):
            cl, _ = rate_limiter.check(user["id"])
            if not cl:
                raise HTTPException(status_code=429, detail="Rate limit exceeded")
            request.state.user = user
            return user

    raise HTTPException(status_code=401, detail="Invalid API key")


async def require_role(role: str):
    """Dependency factory to check role."""
    async def _check(request: Request):
        user = request.state.user
        if not check_role(user, [role, "admin"]):
            raise HTTPException(status_code=403, detail=f"Requires {role} role")
        return user
    return _check


# ── FastAPI app ────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    db_path = os.environ.get("CAGING_DB") or CONFIG.get("service", {}).get("database", "caging.db")
    db_path = os.path.join(BASE_DIR, db_path) if not os.path.isabs(db_path) else db_path
    db.init_db(db_path)

    for uk, uv in CONFIG.get("auth", {}).get("api_keys", {}).items():
        if isinstance(uv, dict):
            role = uv.get("role", "requester")
            system_user = uv.get("system_user", uk)
            api_key = uv.get("key", "")
        else:
            role = "requester"
            system_user = uk
            api_key = uv
        if api_key:
            hashed = bcrypt.hashpw(api_key.encode(), bcrypt.gensalt()).decode()
            existing = db.get_user(uk)
            if existing:
                db.update_user_key(uk, hashed)
                print(f"[seed] Updated key for user '{uk}'")
            else:
                db.create_user(uk, role, hashed, system_user)
                print(f"[seed] Created user '{uk}' with role '{role}'")

    if webfacade._scheduler:
        webfacade._scheduler.start()
    if webfacade._escalation_mgr.enabled:
        webfacade._escalation_mgr.start_polling()
    yield
    if webfacade._scheduler:
        webfacade._scheduler.stop()
    if webfacade._escalation_mgr:
        webfacade._escalation_mgr.stop_polling()


app = FastAPI(title="Caging", version="1.0.0", lifespan=lifespan)

templates_dir = os.path.join(BASE_DIR, "templates")
templates = Jinja2Templates(directory=templates_dir)

static_dir = os.path.join(BASE_DIR, "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.mount("/ui/static", StaticFiles(directory=static_dir), name="ui_static")


# ════════════════════════════════════════════════════════════════════
#  API Endpoints — thin HTTP wrappers over WebFacade
# ════════════════════════════════════════════════════════════════════

# ── Health ────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return webfacade.health()


# ── Exec ──────────────────────────────────────────────────────────
@app.post("/exec")
async def exec_command(
    request: Request,
    body: dict,
    user: dict = Depends(verify_api_key_dependency),
):
    try:
        return await webfacade.exec_command(body, user)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        tb = traceback.format_exc()
        logger.error("Unexpected error in exec_command: %s\n%s", e, tb)
        raise HTTPException(status_code=500, detail=str(e))


# ── Protect ───────────────────────────────────────────────────────
@app.post("/protect")
async def protect_resource(
    request: Request,
    body: dict,
    user: dict = Depends(verify_api_key_dependency),
):
    try:
        return webfacade.protect_resource(body, user)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        tb = traceback.format_exc()
        logger.error("Unexpected error in protect_resource: %s\n%s", e, tb)
        raise HTTPException(status_code=500, detail=str(e))


# ── Release ───────────────────────────────────────────────────────
@app.post("/release")
async def release_resource(
    request: Request,
    body: dict,
    user: dict = Depends(verify_api_key_dependency),
):
    try:
        return webfacade.release_resource(body, user)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        tb = traceback.format_exc()
        logger.error("Unexpected error in release_resource: %s\n%s", e, tb)
        raise HTTPException(status_code=500, detail=str(e))


# ── Approve / Reject / Delegate ───────────────────────────────────
# (All approve/delete/admin endpoints moved to /ui/ below)


# ── Parent callback ──────────────────────────────────────────────
@app.post("/ui/parent-callback")
async def parent_callback(body: dict):
    """
    Receive callback from parent layer when an escalated request completes.

    The parent calls this endpoint (the callback_url we provided during
    escalation) with the result.  We then look up the original escalated
    request, update its status, and forward the callback to the original
    child requester.
    """
    try:
        return webfacade.handle_parent_callback(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        tb = traceback.format_exc()
        logger.error("Unexpected error in parent_callback: %s\n%s", e, tb)
        raise HTTPException(status_code=500, detail=str(e))


# ── List requests ─────────────────────────────────────────────────
# (Moved to /ui/requests below)


# ── Status ────────────────────────────────────────────────────────
@app.get("/status/{request_id}")
async def get_status(
    request_id: str,
    user: dict = Depends(verify_api_key_dependency),
):
    try:
        return webfacade.get_status(request_id, user=user)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        tb = traceback.format_exc()
        logger.error("Unexpected error in get_status: %s\n%s", e, tb)
        raise HTTPException(status_code=500, detail=str(e))


# ════════════════════════════════════════════════════════════════════
#  Web UI
# ════════════════════════════════════════════════════════════════════

@app.get("/ui/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html")


@app.get("/ui/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    return templates.TemplateResponse(request, "user_dashboard.html")


@app.post("/ui/login")
async def login_action(request: Request):
    body = await request.json()
    try:
        result = webfacade.login(body)
        resp = JSONResponse({
            "status": "ok",
            "client_id": result["client_id"],
            "role": result["role"],
        })
        resp.set_cookie(
            "cadmin-session-id", result["session_token"],
            httponly=True, samesite="lax", path="/",
            max_age=86400,  # 24 hours
        )
        return resp
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    except Exception as e:
        tb = traceback.format_exc()
        logger.error("Unexpected error in login_action: %s\n%s", e, tb)
        raise HTTPException(status_code=500, detail=f"Login error: {type(e).__name__}: {e}")


# ════════════════════════════════════════════════════════════════════
#  WebSocket
# ════════════════════════════════════════════════════════════════════

@app.websocket("/ws/notifications")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket with cookie auth from handshake request."""
    token = websocket.cookies.get("cadmin-session-id", "")
    if not token:
        await websocket.close(code=4001)
        return
    # Verify HMAC signature
    client_id = verify_session_token(token)
    if not client_id:
        await websocket.close(code=4001)
        return
    await ws_manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)


# ════════════════════════════════════════════════════════════════════
#  Chat — AI assistant endpoint (pure AI, no built-in commands)
# ════════════════════════════════════════════════════════════════════

@app.post("/chat")
async def chat_endpoint(
    request: Request,
    body: dict,
    user: dict = Depends(verify_session_dependency),
):
    """AI Chat endpoint — plain-text messages only (no ``?``/``>`` commands).

    The response is a plain JSON object: ``{"message": "...", "type": "chat"}``.
    Built-in commands (``?`` / ``>``) are handled client-side in ``chat.js``.
    """
    try:
        message      = body.get("message", "").strip()
        chat_history = body.get("chat_history", [])
        context      = body.get("context", "")

        if not message:
            raise HTTPException(status_code=400, detail="Missing 'message' field")

        response = ai_chat.chat(message, chat_history, context)
        return {"message": response, "type": "chat"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        tb = traceback.format_exc()
        logger.error("Unexpected error in chat_endpoint: %s\n%s", e, tb)
        raise HTTPException(status_code=500, detail=str(e))


# ════════════════════════════════════════════════════════════════════
#  Unified UI API — single POST endpoint for all data operations
#  Auth: Bearer token in Authorization header (session-based)
# ════════════════════════════════════════════════════════════════════

@app.post("/ui/api")
async def ui_api(request: Request, user: dict = Depends(verify_session_dependency)):
    """
    Unified UI API. Accepts JSON body: { action, params }
    Action list: list_requests, get_request_detail, approve_request,
    approve_topic, ai_risk_analysis, reload_policy, list_policies,
    add_policy, update_policy, delete_policy, import_policy_yaml,
    list_auto_approve_topics, add_auto_approve_topic, remove_auto_approve_topic,
    approve_all, delete_all, logout
    """
    body = await request.json()
    action = body.get("action", "")
    params = body.get("params", {})
    session_token = _get_session_token(request)

    try:
        if action == "list_requests":
            data = webfacade.list_requests_for_ui(session_token, params.get("status"))
            return {"ok": True, "data": data["requests"]}
        elif action == "get_request_detail":
            data = webfacade.get_request_detail(session_token, params["request_id"])
            data["user"] = {"id": user["id"], "role": user["role"]}
            return {"ok": True, "data": data}
        elif action == "approve_request":
            body = {"status": params.get("status", "approved"), "note": params.get("note", ""), "delegate_to": params.get("delegate_to")}
            result = await webfacade.approve_request(params["request_id"], body, user)
            return {"ok": True, "data": result}
        elif action == "approve_topic":
            result = await webfacade.approve_topic_from_request(session_token, params["request_id"])
            return {"ok": True, "data": result}
        elif action == "ai_risk_analysis":
            result = webfacade.ai_risk_analysis(params["request_id"], user)
            return {"ok": True, "data": result}
        elif action == "reload_policy":
            return {"ok": True, "data": webfacade.reload_policy(user)}
        elif action == "list_policies":
            return {"ok": True, "data": webfacade.list_policy_rules(user)}
        elif action == "add_policy":
            return {"ok": True, "data": webfacade.add_policy_rule(user, params)}
        elif action == "update_policy":
            rule_id = int(params["rule_id"])
            return {"ok": True, "data": webfacade.update_policy_rule(user, rule_id, params)}
        elif action == "delete_policy":
            rule_id = int(params["rule_id"])
            return {"ok": True, "data": webfacade.delete_policy_rule(user, rule_id)}
        elif action == "import_policy_yaml":
            return {"ok": True, "data": webfacade.import_policy_yaml(user, params)}
        elif action == "list_auto_approve_topics":
            return {"ok": True, "data": webfacade.list_auto_approve_topics(session_token)}
        elif action == "add_auto_approve_topic":
            topic = params.get("topic", "").strip().lower()
            if not topic:
                return {"ok": False, "error": "Topic is required"}
            return {"ok": True, "data": webfacade.add_auto_approve_topic(session_token, topic)}
        elif action == "remove_auto_approve_topic":
            topic = params.get("topic", "")
            webfacade.remove_auto_approve_topic(session_token, topic)
            return {"ok": True, "data": {"status": "removed", "topic": topic}}
        elif action == "approve_all":
            result = await webfacade.approve_all_awaiting(user)
            return {"ok": True, "data": result}
        elif action == "delete_all":
            result = await webfacade.delete_all_requests(user)
            return {"ok": True, "data": result}
        elif action == "logout":
            resp = JSONResponse({"ok": True})
            resp.delete_cookie("cadmin-session-id", path="/")
            return resp
        elif action == "get_current_user":
            return {"ok": True, "data": {"id": user["id"], "role": user["role"]}}
        elif action == "get_dashboard_page":
            html = templates.get_template("user_dashboard.html").render(request=request)
            return {"ok": True, "data": {"html": html}}
        elif action == "get_request_detail_page":
            request_id = params.get("request_id", "")
            if not request_id:
                return {"ok": False, "error": "Missing request_id"}
            html = templates.get_template("detail.html").render(request=request)
            return {"ok": True, "data": {"html": html, "request_id": request_id}}
        else:
            return {"ok": False, "error": f"Unknown action: {action}"}
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        tb = traceback.format_exc()
        logger.error("Unexpected error in ui_api(%s): %s\n%s", action, e, tb)
        return {"ok": False, "error": f"Internal error: {type(e).__name__}: {e}"}
