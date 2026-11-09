"""VPS Auth- & Licensing-Server, Server-Vertrag v2.

Oeffentlich (Tailscale Funnel, nur Pfad /api):
    GET  /api/health
    GET  /api/v1/public-key
    POST /api/v1/verify          -> Ed25519-signiertes Token
Nur im Tailnet:
    /admin ...                   -> Dashboard (Cookie-Anmeldung, CSRF-Schutz)
"""

import html
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, Form, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .database import (
    create_license,
    delete_license,
    get_audit_logs,
    get_license_by_key,
    init_db,
    list_licenses,
    log_audit,
    record_device_verification,
    reset_license_devices,
    update_license_status,
)
from .models import VerifyRequest, VerifyResponse
from .security import (
    AdminSessions,
    Limiters,
    client_ip,
    constant_time_equals,
    load_settings,
)
from .signing import Signer

log = logging.getLogger("uvicorn.error")

VERSION = "2.0.0"
TOKEN_GRACE_SECONDS = 48 * 3600  # Offline-Puffer des Clients
MAX_BODY_BYTES = 8 * 1024
SESSION_COOKIE = "admin_session"
SESSION_TTL_SECONDS = 8 * 3600
VALID_STATUSES = {"active", "suspended"}
TOOL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,100}$")

# Kein Geheimnis: erlaubte Host-Header (Tailnet-Name, lokal fuer Health-Check).
ALLOWED_HOSTS = [
    h.strip().lower()
    for h in os.getenv("ALLOWED_HOSTS", "vps.tail5c3478.ts.net,127.0.0.1,localhost").split(",")
    if h.strip()
]

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


class ApiError(Exception):
    """Fehler mit festem Code (Vertrag v2): Antwort ist {"error": CODE, "detail": Text}."""

    def __init__(self, status_code: int, code: str, detail: str, headers: Optional[Dict[str, str]] = None):
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.headers = headers or {}


def rate_limited(wait_seconds: int) -> ApiError:
    return ApiError(
        429,
        "RATE_LIMITED",
        "Zu viele Anfragen. Bitte spaeter erneut versuchen.",
        {"Retry-After": str(wait_seconds)},
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fehlt etwas Wichtiges, bricht der Start hier ab - lieber kein Server als ein offener.
    settings = load_settings()
    app.state.settings = settings
    app.state.signer = Signer.load_or_create(settings.data_dir / "signing_key.pem")
    app.state.sessions = AdminSessions(ttl_seconds=SESSION_TTL_SECONDS)
    app.state.limiters = Limiters.default()
    init_db()
    log.info("Lizenzserver %s bereit. Oeffentlicher Schluessel (Base64): %s", VERSION, app.state.signer.public_key_b64)
    yield


# Keine automatische API-Dokumentation: weniger Angriffsflaeche und keine Schema-Auskunft.
app = FastAPI(
    title="VPS Auth & Licensing Server",
    version=VERSION,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)


@app.middleware("http")
async def harden_responses(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/"):
        length = request.headers.get("content-length", "")
        if length.isdigit() and int(length) > MAX_BODY_BYTES:
            return JSONResponse({"error": "BAD_REQUEST", "detail": "Anfrage zu gross."}, status_code=413)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if path.startswith("/admin"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
    return response


@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError):
    return JSONResponse(
        {"error": exc.code, "detail": exc.detail}, status_code=exc.status_code, headers=exc.headers
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    # Bewusst ohne "input": Das waeren die vom Aufrufer gesendeten Werte (z. B. Lizenzschluessel).
    errors = [
        {"loc": [str(p) for p in e.get("loc", ())], "msg": str(e.get("msg", "")), "type": str(e.get("type", ""))}
        for e in exc.errors()
    ]
    return JSONResponse({"error": "BAD_REQUEST", "detail": errors}, status_code=422)


# --- Oeffentliche API --------------------------------------------------------

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "service": "vps-auth-server",
        "version": VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/v1/public-key")
async def public_key(request: Request):
    """Oeffentlicher Ed25519-Schluessel (zum Eintragen in den Client)."""
    return {"alg": "Ed25519", "public_key_b64": request.app.state.signer.public_key_b64}


def _license_expiry_ts(lic: dict) -> int:
    """Ablauf der Lizenz als Unix-Sekunden, 0 = unbegrenzt."""
    raw = lic.get("expires_at")
    if not raw:
        return 0
    try:
        return int(datetime.fromisoformat(raw).timestamp())
    except ValueError:
        log.error("Lizenz %s hat ein unlesbares Ablaufdatum: %r", lic.get("id"), raw)
        return 1  # fail closed: gilt als laengst abgelaufen


@app.post("/api/v1/verify", response_model=VerifyResponse)
async def verify_license(req: VerifyRequest, request: Request):
    """Zentrale Lizenz- und Hardware-Pruefung (Node-Locking) mit signiertem Token."""
    limiters: Limiters = request.app.state.limiters
    ip = client_ip(request.headers, request.client.host if request.client else None)

    wait = max(limiters.verify_fail.retry_after(ip), limiters.verify_all.check_and_hit(ip))
    if wait:
        raise rate_limited(wait)

    key_for_log = req.license_key.strip()[:40]
    clean_hwid = req.hwid.strip().upper()
    lic = get_license_by_key(req.license_key)

    if not lic:
        limiters.verify_fail.hit(ip)
        log_audit(key_for_log, clean_hwid, req.tool_id, "REJECT_INVALID_KEY", ip, "Key nicht gefunden")
        raise ApiError(404, "INVALID_KEY", "Lizenzschluessel ungueltig oder nicht gefunden.")

    if lic["status"] != "active":
        log_audit(key_for_log, clean_hwid, req.tool_id, "REJECT_SUSPENDED", ip, f"Status: {lic['status']}")
        raise ApiError(403, "LICENSE_SUSPENDED", "Diese Lizenz wurde gesperrt. Bitte den Support kontaktieren.")

    if lic["tool_id"] != "all" and lic["tool_id"] != req.tool_id:
        log_audit(key_for_log, clean_hwid, req.tool_id, "REJECT_WRONG_TOOL", ip, f"Erfordert: {lic['tool_id']}")
        raise ApiError(403, "WRONG_TOOL", "Diese Lizenz gilt nicht fuer dieses Programm.")

    now = datetime.now(timezone.utc)
    now_ts = int(now.timestamp())
    lic_exp = _license_expiry_ts(lic)
    days_left = None
    if lic_exp:
        if now_ts >= lic_exp:
            log_audit(key_for_log, clean_hwid, req.tool_id, "REJECT_EXPIRED", ip, "Abgelaufen")
            raise ApiError(403, "LICENSE_EXPIRED", "Diese Lizenz ist abgelaufen.")
        days_left = max(0, (lic_exp - now_ts) // 86400)

    bound_hwids = [d["hwid"] for d in lic["devices"]]
    if clean_hwid in bound_hwids:
        record_device_verification(lic["id"], clean_hwid, req.device_name, ip)
        log_audit(key_for_log, clean_hwid, req.tool_id, "VERIFY_OK_EXISTING", ip)
    elif len(bound_hwids) < lic["max_devices"]:
        record_device_verification(lic["id"], clean_hwid, req.device_name, ip)
        log_audit(key_for_log, clean_hwid, req.tool_id, "NEW_DEVICE_BOUND", ip,
                  f"Geraet {len(bound_hwids) + 1}/{lic['max_devices']}")
    else:
        log_audit(key_for_log, clean_hwid, req.tool_id, "REJECT_DEVICE_LIMIT", ip,
                  f"Limit {len(bound_hwids)}/{lic['max_devices']}. Gebunden: {', '.join(bound_hwids)}")
        raise ApiError(
            403,
            "DEVICE_LIMIT_REACHED",
            "Diese Lizenz ist bereits an einen anderen Computer gebunden. "
            "Bitte beim Anbieter einen Hardware-Reset anfordern.",
        )

    exp = now_ts + TOKEN_GRACE_SECONDS
    if lic_exp:
        exp = min(exp, lic_exp)
    token = request.app.state.signer.sign_token(
        {
            "v": 1,
            "hwid": clean_hwid,
            "tool": req.tool_id,
            "nonce": req.nonce,
            "iat": now_ts,
            "exp": exp,
            "lic_exp": lic_exp,
        }
    )
    return VerifyResponse(
        valid=True,
        client_name=lic["client_name"],
        tool_id=lic["tool_id"],
        status="active",
        expires_at=lic["expires_at"],
        days_left=days_left,
        token=token,
        message="Lizenz verifiziert.",
    )


# --- Admin-Dashboard (nur im Tailnet) -----------------------------------------

LOGIN_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Admin Login</title>
<style>
  body {{ background:#0b0f17; color:#fff; font-family:sans-serif; display:flex; align-items:center; justify-content:center; height:100vh; margin:0; }}
  .card {{ background:#141a24; border:1px solid #222b3a; border-radius:8px; padding:28px; width:340px; }}
  input {{ width:100%; box-sizing:border-box; background:#0d121a; border:1px solid #222b3a; color:#fff; padding:10px; border-radius:6px; margin:12px 0; }}
  button {{ width:100%; background:#2962ff; border:none; color:#fff; padding:10px; border-radius:6px; font-weight:bold; cursor:pointer; }}
  .err {{ color:#ff6b6b; font-size:13px; }}
</style></head>
<body><div class="card">
  <h3 style="margin-top:0;">Auth-Server Admin</h3>
  <p style="font-size:13px; color:#8fa0be;">Bitte Admin-Token eingeben:</p>
  {error}
  <form method="POST" action="/admin/login">
    <input type="password" name="token" placeholder="AUTH_ADMIN_TOKEN" autocomplete="current-password" autofocus required>
    <button type="submit">Anmelden</button>
  </form>
</div></body></html>"""


def login_page(error: str = "", status_code: int = 401, headers: Optional[Dict[str, str]] = None) -> HTMLResponse:
    notice = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return HTMLResponse(LOGIN_PAGE.format(error=notice), status_code=status_code, headers=headers)


def _client(request: Request) -> str:
    return client_ip(request.headers, request.client.host if request.client else None)


def _bearer_ok(request: Request) -> Optional[bool]:
    """None = kein Bearer-Header; sonst Ergebnis der Pruefung. Fehlversuche werden gezaehlt."""
    header = request.headers.get("authorization")
    if not header:
        return None
    limiter = request.app.state.limiters.admin_fail
    ip = _client(request)
    wait = limiter.retry_after(ip)
    if wait:
        raise rate_limited(wait)
    scheme, _, value = header.partition(" ")
    ok = scheme.lower() == "bearer" and constant_time_equals(value.strip(), request.app.state.settings.admin_token)
    if not ok:
        limiter.hit(ip)
    return ok


def _session_csrf(request: Request) -> Optional[str]:
    return request.app.state.sessions.csrf_for(request.cookies.get(SESSION_COOKIE))


def require_admin(request: Request, csrf: str = "") -> None:
    """Erlaubt Bearer-Header ODER gueltige Cookie-Sitzung mit passendem CSRF-Feld."""
    bearer = _bearer_ok(request)
    if bearer:
        return
    expected = _session_csrf(request)
    if bearer is False or expected is None:
        raise ApiError(401, "ADMIN_AUTH_REQUIRED", "Bitte am Admin-Dashboard anmelden.")
    if not csrf or not constant_time_equals(csrf, expected):
        raise ApiError(403, "CSRF_INVALID", "Ungueltiges Formular-Token. Bitte die Seite neu laden.")


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    bearer = _bearer_ok(request)
    csrf = ""
    if not bearer:
        expected = _session_csrf(request)
        if bearer is False or expected is None:
            return login_page()
        csrf = expected

    all_lics = list_licenses()
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "csrf": csrf,
            "host": request.headers.get("host", ""),
            "licenses": all_lics,
            "total_licenses": len(all_lics),
            "active_licenses": sum(1 for lic in all_lics if lic["status"] == "active"),
            "total_devices": sum(len(lic["devices"]) for lic in all_lics),
            "audit_logs": get_audit_logs(limit=30),
        },
    )


@app.post("/admin/login")
async def admin_login(request: Request, token: str = Form("")):
    limiter = request.app.state.limiters.admin_fail
    ip = _client(request)
    wait = limiter.retry_after(ip)
    if wait:
        return login_page("Zu viele Fehlversuche. Bitte spaeter erneut versuchen.", 429, {"Retry-After": str(wait)})

    settings = request.app.state.settings
    if not constant_time_equals(token.strip(), settings.admin_token):
        limiter.hit(ip)
        log_audit(None, None, None, "ADMIN_LOGIN_FAILED", ip)
        return login_page("Ungueltiges Token.")

    log_audit(None, None, None, "ADMIN_LOGIN_OK", ip)
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        request.app.state.sessions.create(),
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/admin",
    )
    return response


@app.post("/admin/logout")
async def admin_logout(request: Request, csrf: str = Form("")):
    require_admin(request, csrf)
    request.app.state.sessions.delete(request.cookies.get(SESSION_COOKIE))
    response = RedirectResponse("/admin", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/admin")
    return response


@app.post("/admin/create")
async def admin_create_license(
    request: Request,
    csrf: str = Form(""),
    client_name: str = Form(...),
    tool_id: str = Form("tradingview_ai"),
    days_valid: int = Form(30),
    max_devices: int = Form(1),
    notes: str = Form(""),
):
    require_admin(request, csrf)
    name = client_name.strip()
    if not 2 <= len(name) <= 150:
        raise ApiError(422, "BAD_REQUEST", "Name: 2 bis 150 Zeichen.")
    if not TOOL_ID_PATTERN.match(tool_id):
        raise ApiError(422, "BAD_REQUEST", "Ungueltige Tool-ID.")
    if not 0 <= days_valid <= 3650:
        raise ApiError(422, "BAD_REQUEST", "Gueltigkeit: 0 (unbegrenzt) bis 3650 Tage.")
    if not 1 <= max_devices <= 10:
        raise ApiError(422, "BAD_REQUEST", "Geraete-Limit: 1 bis 10.")

    lic = create_license(name, tool_id, None if days_valid <= 0 else days_valid, max_devices, notes.strip()[:500])
    log_audit(lic["license_key"], None, tool_id, "ADMIN_CREATE_LICENSE", _client(request), f"Kunde: {name}")
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/reset-devices")
async def admin_reset_devices(request: Request, csrf: str = Form(""), license_key: str = Form(...)):
    require_admin(request, csrf)
    count = reset_license_devices(license_key)
    log_audit(license_key, None, None, "ADMIN_RESET_DEVICES", _client(request), f"{count} Geraete freigegeben")
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/toggle-status")
async def admin_toggle_status(
    request: Request, csrf: str = Form(""), license_key: str = Form(...), new_status: str = Form(...)
):
    require_admin(request, csrf)
    if new_status not in VALID_STATUSES:
        raise ApiError(422, "BAD_REQUEST", "Status muss 'active' oder 'suspended' sein.")
    update_license_status(license_key, new_status)
    log_audit(license_key, None, None, f"ADMIN_STATUS_{new_status.upper()}", _client(request))
    return RedirectResponse("/admin", status_code=303)


@app.post("/admin/delete")
async def admin_delete_license(request: Request, csrf: str = Form(""), license_key: str = Form(...)):
    require_admin(request, csrf)
    delete_license(license_key)
    log_audit(license_key, None, None, "ADMIN_DELETE_LICENSE", _client(request))
    return RedirectResponse("/admin", status_code=303)
