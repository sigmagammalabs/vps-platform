import hashlib
import hmac
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Header, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

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
from .models import CreateLicenseRequest, VerifyRequest, VerifyResponse

AUTH_ADMIN_TOKEN = os.getenv("AUTH_ADMIN_TOKEN", "sglabs-admin-secure-token-2026")
AUTH_JWT_SECRET = os.getenv("AUTH_JWT_SECRET", "salt-key-sigma-gamma-labs-2026").encode("utf-8")
ALLOWED_HOSTS = [h.strip().lower() for h in os.getenv("ALLOWED_HOSTS", "vps.tail5c3478.ts.net,127.0.0.1,localhost").split(",")]

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="VPS Auth & Licensing Server",
    description="Zentrale Hardware-Aktivierung und Node-Locking für TradingView AI und Tools",
    version="1.0.0",
    lifespan=lifespan,
)


def compute_offline_token(license_key: str, hwid: str, exp_timestamp: int) -> str:
    """Erzeugt ein HMAC-signiertes Offline-Grace-Token (z. B. 48h gültig)."""
    payload = f"{license_key}:{hwid}:{exp_timestamp}"
    sig = hmac.new(AUTH_JWT_SECRET, payload.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    return f"GRACE-{exp_timestamp}-{sig}"


def verify_admin_auth(req: Request, token_param: Optional[str] = None) -> bool:
    """Prüft, ob der Request vom berechtigten Administrator stammt."""
    if token_param and token_param == AUTH_ADMIN_TOKEN:
        return True
    auth_header = req.headers.get("Authorization")
    if auth_header and auth_header.replace("Bearer ", "").strip() == AUTH_ADMIN_TOKEN:
        return True
    return False


# --- Öffentliche Health-Prüfung ----------------------------------------------

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "service": "vps-auth-server",
        "version": "1.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# --- Client-Verifikations-API (Wird vom Desktop-Tool aufgerufen) -------------

@app.post("/api/v1/verify", response_model=VerifyResponse)
async def verify_license(req: VerifyRequest, request: Request):
    """Zentrale Lizenz- und Hardware-Prüfung.

    Wird beim Start der Desktop-App (z. B. TradingView AI) aufgerufen.
    Prüft Gültigkeit, Status und bindet die Hardware-ID (Node-Locking).
    Verhindert die Weitergabe der Software auf fremde Computer.
    """
    client_ip = request.client.host if request.client else "unknown"
    lic = get_license_by_key(req.license_key)

    if not lic:
        log_audit(req.license_key, req.hwid, req.tool_id, "REJECT_INVALID_KEY", client_ip, "Key nicht gefunden")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Lizenzschlüssel ungültig oder nicht gefunden.",
        )

    # 1. Status prüfen
    if lic["status"] != "active":
        log_audit(req.license_key, req.hwid, req.tool_id, "REJECT_SUSPENDED", client_ip, f"Status: {lic['status']}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Diese Lizenz wurde gesperrt ({lic['status']}). Bitte wenden Sie sich an den Support.",
        )

    # 2. Tool-Berechtigung prüfen
    if lic["tool_id"] != "all" and lic["tool_id"] != req.tool_id:
        log_audit(req.license_key, req.hwid, req.tool_id, "REJECT_WRONG_TOOL", client_ip, f"Erfordert: {lic['tool_id']}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Diese Lizenz ist nur für '{lic['tool_id']}' freigeschaltet, nicht für '{req.tool_id}'.",
        )

    # 3. Ablaufdatum prüfen
    days_left = None
    if lic["expires_at"]:
        try:
            exp_dt = datetime.fromisoformat(lic["expires_at"])
            now_dt = datetime.now(timezone.utc)
            if now_dt > exp_dt:
                log_audit(req.license_key, req.hwid, req.tool_id, "REJECT_EXPIRED", client_ip, "Abgelaufen")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Diese Lizenz ist am {exp_dt.strftime('%d.%m.%Y')} abgelaufen.",
                )
            days_left = max(0, (exp_dt - now_dt).days)
        except ValueError:
            pass

    # 4. Hardware-Prüfung & Node-Locking (Bindung an Hardware-ID)
    bound_hwids = [d["hwid"] for d in lic["devices"]]
    clean_hwid = req.hwid.strip().upper()

    if clean_hwid in bound_hwids:
        # Gerät bereits gebunden -> OK, Timestamp aktualisieren
        record_device_verification(lic["id"], clean_hwid, req.device_name, client_ip)
        log_audit(req.license_key, clean_hwid, req.tool_id, "VERIFY_OK_EXISTING", client_ip)
    else:
        # Neues Gerät -> Prüfen, ob noch Platz frei ist
        if len(bound_hwids) < lic["max_devices"]:
            record_device_verification(lic["id"], clean_hwid, req.device_name, client_ip)
            log_audit(req.license_key, clean_hwid, req.tool_id, "NEW_DEVICE_BOUND", client_ip, f"Gerät {len(bound_hwids)+1}/{lic['max_devices']}")
        else:
            # Geräte-Limit erreicht -> WEITERGABE BLOCKIERT!
            log_audit(
                req.license_key,
                clean_hwid,
                req.tool_id,
                "REJECT_DEVICE_LIMIT",
                client_ip,
                f"Limit erreicht ({len(bound_hwids)}/{lic['max_devices']}). Bereits gebunden: {', '.join(bound_hwids)}",
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"Weitergabe blockiert! Diese Lizenz ist bereits an einen anderen Computer gebunden "
                    f"(Maximale Geräteanzahl: {lic['max_devices']}). Bitte fordern Sie beim Entwickler einen Hardware-Reset an."
                ),
            )

    # 5. Offline-Grace-Token (48 Stunden gültig)
    now_ts = int(datetime.now(timezone.utc).timestamp())
    grace_exp = now_ts + (48 * 3600)
    token = compute_offline_token(req.license_key, clean_hwid, grace_exp)

    return VerifyResponse(
        valid=True,
        client_name=lic["client_name"],
        tool_id=lic["tool_id"],
        status="active",
        expires_at=lic["expires_at"],
        days_left=days_left,
        token=token,
        message="Lizenz erfolgreich verifiziert. Hardware gebunden.",
    )


# --- Admin Web-Dashboard -----------------------------------------------------

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, token: Optional[str] = None):
    # Falls kein Token in URL oder Header, Passwort-Eingabe zeigen
    if not verify_admin_auth(request, token):
        return HTMLResponse("""
        <!DOCTYPE html>
        <html>
        <head><meta charset="utf-8"><title>Admin Login</title>
        <style>
          body { background: #0b0f17; color: #fff; font-family: sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
          .card { background: #141a24; border: 1px solid #222b3a; border-radius: 8px; padding: 28px; width: 340px; }
          input { width: 100%; box-sizing: border-box; background: #0d121a; border: 1px solid #222b3a; color: #fff; padding: 10px; border-radius: 6px; margin: 12px 0; }
          button { width: 100%; background: #2962ff; border: none; color: #fff; padding: 10px; border-radius: 6px; font-weight: bold; cursor: pointer; }
        </style>
        </head>
        <body>
          <div class="card">
            <h3 style="margin-top:0;">🛡️ Auth-Server Admin</h3>
            <p style="font-size: 13px; color: #8fa0be;">Bitte Admin-Token eingeben:</p>
            <form method="GET" action="/admin">
              <input type="password" name="token" placeholder="AUTH_ADMIN_TOKEN" autofocus required>
              <button type="submit">Anmelden</button>
            </form>
          </div>
        </body></html>
        """, status_code=401)

    all_lics = list_licenses()
    active_count = sum(1 for l in all_lics if l["status"] == "active")
    total_devs = sum(len(l["devices"]) for l in all_lics)
    logs = get_audit_logs(limit=30)

    return templates.TemplateResponse(
        "admin.html",
        {
            "request": request,
            "token": token,
            "host": request.headers.get("host", "vps.tail5c3478.ts.net:8100"),
            "licenses": all_lics,
            "total_licenses": len(all_lics),
            "active_licenses": active_count,
            "total_devices": total_devs,
            "audit_logs": logs,
        },
    )


@app.post("/admin/create")
async def admin_create_license(
    request: Request,
    token: str = Form(...),
    client_name: str = Form(...),
    tool_id: str = Form("all"),
    days_valid: int = Form(30),
    max_devices: int = Form(1),
    notes: str = Form(""),
):
    if not verify_admin_auth(request, token):
        raise HTTPException(status_code=401, detail="Ungültiges Admin-Token")

    days = None if days_valid <= 0 else days_valid
    lic = create_license(client_name, tool_id, days, max_devices, notes)
    log_audit(lic["license_key"], None, tool_id, "ADMIN_CREATE_LICENSE", request.client.host if request.client else None, f"Kunde: {client_name}")
    return RedirectResponse(url=f"/admin?token={token}", status_code=303)


@app.post("/admin/reset-devices")
async def admin_reset_devices(
    request: Request,
    token: str = Form(...),
    license_key: str = Form(...),
):
    if not verify_admin_auth(request, token):
        raise HTTPException(status_code=401, detail="Ungültiges Admin-Token")

    count = reset_license_devices(license_key)
    log_audit(license_key, None, None, "ADMIN_RESET_DEVICES", request.client.host if request.client else None, f"{count} Geräte freigegeben")
    return RedirectResponse(url=f"/admin?token={token}", status_code=303)


@app.post("/admin/toggle-status")
async def admin_toggle_status(
    request: Request,
    token: str = Form(...),
    license_key: str = Form(...),
    new_status: str = Form(...),
):
    if not verify_admin_auth(request, token):
        raise HTTPException(status_code=401, detail="Ungültiges Admin-Token")

    update_license_status(license_key, new_status)
    log_audit(license_key, None, None, f"ADMIN_STATUS_{new_status.upper()}", request.client.host if request.client else None)
    return RedirectResponse(url=f"/admin?token={token}", status_code=303)


@app.post("/admin/delete")
async def admin_delete_license(
    request: Request,
    token: str = Form(...),
    license_key: str = Form(...),
):
    if not verify_admin_auth(request, token):
        raise HTTPException(status_code=401, detail="Ungültiges Admin-Token")

    delete_license(license_key)
    log_audit(license_key, None, None, "ADMIN_DELETE_LICENSE", request.client.host if request.client else None)
    return RedirectResponse(url=f"/admin?token={token}", status_code=303)
