from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .core import (
    ConfigError,
    VPN_CONFIG,
    XRAY_CONFIG,
    atomic_write_json,
    build_openconnect_command,
    manager,
    normalize_http_origin,
    normalize_vpn_route_config,
    parse_trusted_origins,
    read_json,
    validate_xray_shape,
)


STATIC_DIR = Path(__file__).parent / "static"
APP_VERSION = os.getenv("APP_VERSION", "dev")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
SESSION_SECRET = os.environ["SESSION_SECRET"].encode()
COOKIE_NAME = "x2c_session"
SESSION_TTL = 12 * 60 * 60
try:
    TRUSTED_ORIGINS = parse_trusted_origins(os.getenv("TRUSTED_ORIGINS", ""))
except ConfigError as exc:
    raise RuntimeError(f"TRUSTED_ORIGINS 配置无效: {exc}") from exc
if len(ADMIN_PASSWORD) < 12:
    raise RuntimeError("ADMIN_PASSWORD 至少需要 12 个字符")
if len(SESSION_SECRET) < 32:
    raise RuntimeError("SESSION_SECRET 至少需要 32 个字符")

LOGIN_ATTEMPTS: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=10))
LOGIN_LOCK = threading.Lock()
LOGIN_WINDOW = 5 * 60
LOGIN_LIMIT = 5


def _sign(value: str) -> str:
    return hmac.new(SESSION_SECRET, value.encode(), hashlib.sha256).hexdigest()


def _make_session(username: str) -> str:
    expires = int(time.time()) + SESSION_TTL
    payload = f"{username}|{expires}"
    return f"{payload}|{_sign(payload)}"


def _session_user(token: str | None) -> str | None:
    if not token:
        return None
    try:
        username, expires, signature = token.rsplit("|", 2)
        payload = f"{username}|{expires}"
        if not hmac.compare_digest(signature, _sign(payload)) or int(expires) < time.time():
            return None
        return username
    except (ValueError, TypeError):
        return None


def require_user(request: Request) -> str:
    user = _session_user(request.cookies.get(COOKIE_NAME))
    if not user:
        raise HTTPException(status_code=401, detail="请先登录")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin:
            try:
                request_origin = normalize_http_origin(origin)
                local_origin = normalize_http_origin(str(request.base_url))
            except ConfigError as exc:
                raise HTTPException(status_code=403, detail="请求来源无效") from exc
            if request_origin not in TRUSTED_ORIGINS and request_origin != local_origin:
                raise HTTPException(status_code=403, detail="请求来源无效")
    return user


class LoginBody(BaseModel):
    username: str
    password: str


class ConnectBody(BaseModel):
    password: str = ""
    otp: str = ""


class VpnConfigBody(BaseModel):
    config: dict[str, Any]
    save_password: bool = False


@asynccontextmanager
async def lifespan(_: FastAPI):
    manager.ensure_direct_fallback()
    try:
        vpn = read_json(VPN_CONFIG)
        if vpn.get("autostart") and vpn.get("password"):
            manager.start_vpn()
    except Exception as exc:
        manager.services["vpn"].logs.append(f"自动连接提示: {exc}")
    try:
        xray = read_json(XRAY_CONFIG)
        if xray.get("inbounds"):
            manager.start_xray()
    except Exception as exc:
        manager.services["xray"].logs.append(f"自动启动提示: {exc}")
    yield
    manager.stop("xray")
    manager.stop("vpn")


app = FastAPI(title="proxy2openconnect", version=APP_VERSION, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(ConfigError)
async def config_error_handler(_: Request, exc: ConfigError):
    return Response(
        content=json.dumps({"detail": str(exc)}, ensure_ascii=False),
        status_code=400,
        media_type="application/json",
    )


@app.get("/health")
def health():
    return {"ok": True, "version": APP_VERSION}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(STATIC_DIR / "brand" / "favicon.ico", media_type="image/x-icon")


@app.get("/apple-touch-icon.png", include_in_schema=False)
def apple_touch_icon():
    return FileResponse(STATIC_DIR / "brand" / "apple-touch-icon.png", media_type="image/png")


@app.post("/api/login")
def login(body: LoginBody, request: Request, response: Response):
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    with LOGIN_LOCK:
        attempts = LOGIN_ATTEMPTS[client_ip]
        while attempts and attempts[0] < now - LOGIN_WINDOW:
            attempts.popleft()
        if len(attempts) >= LOGIN_LIMIT:
            raise HTTPException(status_code=429, detail="登录失败次数过多，请五分钟后再试")
    valid = hmac.compare_digest(body.username, ADMIN_USERNAME) and hmac.compare_digest(body.password, ADMIN_PASSWORD)
    if not valid:
        with LOGIN_LOCK:
            LOGIN_ATTEMPTS[client_ip].append(now)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    with LOGIN_LOCK:
        LOGIN_ATTEMPTS.pop(client_ip, None)
    response.set_cookie(
        COOKIE_NAME,
        _make_session(body.username),
        max_age=SESSION_TTL,
        httponly=True,
        samesite="strict",
        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
        path="/",
    )
    return {"username": body.username}


@app.post("/api/logout")
def logout(response: Response, _: str = Depends(require_user)):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/me")
def me(user: str = Depends(require_user)):
    return {"username": user}


@app.get("/api/status")
def status(_: str = Depends(require_user)):
    return manager.status()


@app.get("/api/logs/{service}")
def logs(service: str, _: str = Depends(require_user)):
    if service not in {"vpn", "xray"}:
        raise HTTPException(status_code=404, detail="未知服务")
    return {"lines": manager.logs(service)}


@app.get("/api/xray/config")
def get_xray_config(_: str = Depends(require_user)):
    return {"config": read_json(XRAY_CONFIG)}


@app.put("/api/xray/config")
def put_xray_config(config: dict[str, Any] = Body(...), _: str = Depends(require_user)):
    validate_xray_shape(config)
    message = manager.validate_xray(config)
    atomic_write_json(XRAY_CONFIG, config)
    return {"ok": True, "message": message}


@app.post("/api/xray/validate")
def validate_xray(config: dict[str, Any] = Body(...), _: str = Depends(require_user)):
    return {"ok": True, "message": manager.validate_xray(config)}


@app.post("/api/xray/{action}")
def xray_action(action: str, _: str = Depends(require_user)):
    if action == "start":
        manager.start_xray()
    elif action == "stop":
        manager.stop("xray")
    elif action == "restart":
        manager.restart_xray()
    else:
        raise HTTPException(status_code=404, detail="未知操作")
    return {"ok": True}


@app.get("/api/vpn/config")
def get_vpn_config(_: str = Depends(require_user)):
    config = normalize_vpn_route_config(read_json(VPN_CONFIG))
    has_password = bool(config.pop("password", ""))
    return {"config": config, "has_password": has_password}


@app.put("/api/vpn/config")
def put_vpn_config(body: VpnConfigBody, _: str = Depends(require_user)):
    current = read_json(VPN_CONFIG)
    config = body.config.copy()
    supplied_password = str(config.pop("password", ""))
    config["password"] = supplied_password if body.save_password else current.get("password", "")
    config = normalize_vpn_route_config(config)
    build_openconnect_command(config)
    atomic_write_json(VPN_CONFIG, config)
    manager.notify_vpn_config_changed()
    return {"ok": True}


@app.post("/api/vpn/connect")
def vpn_connect(body: ConnectBody, _: str = Depends(require_user)):
    manager.start_vpn(body.password, body.otp)
    return {"ok": True}


@app.post("/api/vpn/disconnect")
def vpn_disconnect(_: str = Depends(require_user)):
    manager.stop("vpn")
    return {"ok": True}


@app.post("/api/vpn/trust-certificate")
def vpn_trust_certificate(_: str = Depends(require_user)):
    candidate = manager.trust_certificate_candidate()
    return {"ok": True, "candidate": candidate}
