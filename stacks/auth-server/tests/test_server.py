"""Tests des Lizenzservers (Server-Vertrag v2). Laufen ohne Netzwerk."""

import base64
import json
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi.testclient import TestClient

from app.security import AdminSessions, ConfigError, SlidingWindowLimiter, client_ip
from app.signing import SigningKeyError

ADMIN_TOKEN = "Zk3pQ9vL2mXw8RtYb5NcH7sD4fGj1aEu"  # 32 Zeichen, nur fuer Tests
HWID = "TVKI-AAAA-BBBB-CCCC-DDDD"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_nonce() -> str:
    return b64url(secrets.token_bytes(16))


def check_token(token: str, public_key_b64: str) -> dict:
    """Prueft die Signatur so wie der Client und liefert den Payload."""
    payload_b64, sig_b64 = token.split(".")
    Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64)).verify(
        b64url_decode(sig_b64), payload_b64.encode("ascii")
    )
    return json.loads(b64url_decode(payload_b64))


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("AUTH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "0")
    return tmp_path


@pytest.fixture
def client(data_dir):
    from app.main import app

    with TestClient(app, base_url="http://localhost") as test_client:
        yield test_client


@pytest.fixture
def public_key(client):
    return client.get("/api/v1/public-key").json()["public_key_b64"]


def make_license(days=30, tool="tradingview_ai", max_devices=1):
    from app.database import create_license

    return create_license("Test Kunde", tool, days, max_devices, "")


def set_db(data_dir, sql, params=()):
    with sqlite3.connect(data_dir / "licenses.db") as conn:
        conn.execute(sql, params)


def verify(client, key, hwid=HWID, tool="tradingview_ai", nonce=None, headers=None):
    body = {"license_key": key, "hwid": hwid, "tool_id": tool, "device_name": "PC", "nonce": nonce or new_nonce()}
    return client.post("/api/v1/verify", json=body, headers=headers or {})


# --- Start / Konfiguration ----------------------------------------------------

@pytest.mark.parametrize(
    "value",
    [None, "", "kurz", "sglabs-admin-secure-token-2026", "hier-ein-starkes-geheimes-admin-passwort-eintragen"],
)
def test_server_refuses_to_start_without_a_strong_admin_token(data_dir, monkeypatch, value):
    from app.main import app

    if value is None:
        monkeypatch.delenv("AUTH_ADMIN_TOKEN")
    else:
        monkeypatch.setenv("AUTH_ADMIN_TOKEN", value)
    with pytest.raises(ConfigError):
        with TestClient(app, base_url="http://localhost"):
            pass


def test_signing_key_is_created_once_and_stays_stable(data_dir):
    from app.main import app

    with TestClient(app, base_url="http://localhost") as first:
        key_1 = first.get("/api/v1/public-key").json()["public_key_b64"]
    assert (data_dir / "signing_key.pem").is_file()
    with TestClient(app, base_url="http://localhost") as second:
        key_2 = second.get("/api/v1/public-key").json()["public_key_b64"]
    assert key_1 == key_2
    assert len(base64.b64decode(key_1)) == 32


def test_unreadable_signing_key_aborts_instead_of_replacing_it(data_dir):
    from app.main import app

    (data_dir / "signing_key.pem").write_text("kaputt")
    with pytest.raises(SigningKeyError):
        with TestClient(app, base_url="http://localhost"):
            pass
    assert (data_dir / "signing_key.pem").read_text() == "kaputt"  # nicht ueberschrieben


def test_docs_and_schema_are_not_exposed(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_foreign_host_header_is_rejected(client):
    assert client.get("/api/health", headers={"Host": "evil.example"}).status_code == 400


def test_oversized_body_is_rejected(client):
    response = client.post("/api/v1/verify", content=b"x" * 9000, headers={"Content-Type": "application/json"})
    assert response.status_code == 413


# --- Verifikation: Erfolg --------------------------------------------------------

def test_verify_returns_valid_signed_token(client, public_key):
    lic = make_license(days=30)
    nonce = new_nonce()
    response = verify(client, lic["license_key"], hwid=HWID.lower(), nonce=nonce)
    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is True and data["client_name"] == "Test Kunde"

    payload = check_token(data["token"], public_key)
    assert payload["v"] == 1
    assert payload["hwid"] == HWID  # in Grossbuchstaben
    assert payload["tool"] == "tradingview_ai"
    assert payload["nonce"] == nonce
    assert 0 < payload["lic_exp"] and payload["exp"] <= payload["lic_exp"]
    assert payload["exp"] - payload["iat"] == 48 * 3600


def test_unlimited_license_has_lic_exp_zero_and_48h_token(client, public_key):
    lic = make_license(days=None)
    payload = check_token(verify(client, lic["license_key"]).json()["token"], public_key)
    assert payload["lic_exp"] == 0
    assert payload["exp"] - payload["iat"] == 48 * 3600


def test_token_expiry_is_capped_by_license_expiry(client, data_dir, public_key):
    lic = make_license(days=30)
    soon = datetime.now(timezone.utc) + timedelta(hours=1)
    set_db(data_dir, "UPDATE licenses SET expires_at = ? WHERE license_key = ?", (soon.isoformat(), lic["license_key"]))
    payload = check_token(verify(client, lic["license_key"]).json()["token"], public_key)
    assert payload["exp"] == payload["lic_exp"] == int(soon.timestamp())


def test_token_signed_by_a_foreign_key_does_not_verify(client, public_key):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    other = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    token = verify(client, make_license()["license_key"]).json()["token"]
    with pytest.raises(InvalidSignature):
        check_token(token, base64.b64encode(other).decode())


# --- Verifikation: Fehlercodes -----------------------------------------------------

def assert_error(response, status, code):
    assert response.status_code == status, response.text
    body = response.json()
    assert body["error"] == code
    assert isinstance(body["detail"], str) and body["detail"]


def test_unknown_key(client):
    assert_error(verify(client, "ACT-0000-0000-0000-0000"), 404, "INVALID_KEY")


def test_suspended_license(client, data_dir):
    lic = make_license()
    set_db(data_dir, "UPDATE licenses SET status = 'suspended' WHERE license_key = ?", (lic["license_key"],))
    assert_error(verify(client, lic["license_key"]), 403, "LICENSE_SUSPENDED")


def test_expired_license(client, data_dir):
    lic = make_license()
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    set_db(data_dir, "UPDATE licenses SET expires_at = ? WHERE license_key = ?", (past, lic["license_key"]))
    assert_error(verify(client, lic["license_key"]), 403, "LICENSE_EXPIRED")


def test_unreadable_expiry_fails_closed(client, data_dir):
    lic = make_license()
    set_db(data_dir, "UPDATE licenses SET expires_at = 'kein-datum' WHERE license_key = ?", (lic["license_key"],))
    assert_error(verify(client, lic["license_key"]), 403, "LICENSE_EXPIRED")


def test_wrong_tool(client):
    lic = make_license(tool="premarket-screener")
    assert_error(verify(client, lic["license_key"], tool="tradingview_ai"), 403, "WRONG_TOOL")


def test_universal_license_works_for_any_tool(client):
    lic = make_license(tool="all")
    assert verify(client, lic["license_key"], tool="arbitrage-scout").status_code == 200


def test_device_limit_and_reset(client):
    lic = make_license(max_devices=1)
    key = lic["license_key"]
    assert verify(client, key, hwid="TVKI-1111-1111-1111-1111").status_code == 200
    assert verify(client, key, hwid="TVKI-1111-1111-1111-1111").status_code == 200  # gleiches Geraet
    assert_error(verify(client, key, hwid="TVKI-2222-2222-2222-2222"), 403, "DEVICE_LIMIT_REACHED")

    from app.database import reset_license_devices

    assert reset_license_devices(key) == 1
    assert verify(client, key, hwid="TVKI-2222-2222-2222-2222").status_code == 200


def test_validation_error_has_contract_shape_and_does_not_echo_input(client):
    secret_looking = "ACT-GEHEIM-EINGABE"
    response = client.post("/api/v1/verify", json={"license_key": secret_looking, "hwid": "kurz"})
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "BAD_REQUEST"
    assert isinstance(body["detail"], list) and body["detail"]
    for item in body["detail"]:
        assert set(item) == {"loc", "msg", "type"} and isinstance(item["loc"], list)
    assert secret_looking not in response.text


def test_missing_nonce_is_rejected(client):
    lic = make_license()
    response = client.post("/api/v1/verify", json={"license_key": lic["license_key"], "hwid": HWID})
    assert response.status_code == 422 and response.json()["error"] == "BAD_REQUEST"


# --- Anfragebremse -------------------------------------------------------------------

def test_guessing_keys_is_throttled_per_address(client):
    for _ in range(10):
        assert verify(client, "ACT-0000-0000-0000-0000").status_code == 404
    blocked = verify(client, "ACT-0000-0000-0000-0000")
    assert_error(blocked, 429, "RATE_LIMITED")
    assert int(blocked.headers["Retry-After"]) > 0

    # eine andere Adresse ist nicht betroffen
    other = verify(client, "ACT-0000-0000-0000-0000", headers={"X-Forwarded-For": "9.9.9.9"})
    assert other.status_code == 404


def test_throttle_uses_last_forwarded_entry_so_spoofing_does_not_help(client):
    spoof = {"X-Forwarded-For": "1.2.3.4, 5.5.5.5"}  # rechts steht, was Tailscale gesehen hat
    for _ in range(10):
        verify(client, "ACT-0000-0000-0000-0000", headers=spoof)
    changed_left = {"X-Forwarded-For": "7.7.7.7, 5.5.5.5"}
    assert verify(client, "ACT-0000-0000-0000-0000", headers=changed_left).status_code == 429


def test_valid_customer_is_not_locked_out_by_foreign_guessing(client):
    for _ in range(10):
        verify(client, "ACT-0000-0000-0000-0000", headers={"X-Forwarded-For": "6.6.6.6"})
    lic = make_license()
    assert verify(client, lic["license_key"], headers={"X-Forwarded-For": "8.8.8.8"}).status_code == 200


def test_overall_request_limit(client):
    client.app.state.limiters.verify_all.limit = 3
    lic = make_license()
    codes = [verify(client, lic["license_key"]).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]


# --- Admin ------------------------------------------------------------------------------

def login(client, token=ADMIN_TOKEN):
    return client.post("/admin/login", data={"token": token}, follow_redirects=False)


def csrf_of(client) -> str:
    page = client.get("/admin").text
    marker = 'name="csrf" value="'
    return page.split(marker, 1)[1].split('"', 1)[0]


def test_admin_requires_login_and_ignores_token_in_url(client):
    assert client.get("/admin").status_code == 401
    assert client.get(f"/admin?token={ADMIN_TOKEN}").status_code == 401  # alter Weg gilt nicht mehr


def test_wrong_login_is_rejected_and_throttled(client):
    for _ in range(5):
        assert login(client, "falsch").status_code == 401
    assert login(client, "falsch").status_code == 429
    assert login(client).status_code == 429  # auch das richtige Token, solange gesperrt


def test_login_sets_strict_httponly_cookie_and_keeps_token_out_of_the_url(client):
    response = login(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin"
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/admin" in cookie
    assert ADMIN_TOKEN not in response.headers["location"] + cookie
    page = client.get("/admin")
    assert page.status_code == 200 and ADMIN_TOKEN not in page.text
    assert page.headers["cache-control"] == "no-store"


def test_admin_actions_need_a_matching_csrf_token(client):
    login(client)
    form = {"client_name": "Neuer Tester", "tool_id": "tradingview_ai", "days_valid": "30", "max_devices": "1"}
    no_csrf = client.post("/admin/create", data=form, follow_redirects=False)
    assert no_csrf.status_code == 403 and no_csrf.json()["error"] == "CSRF_INVALID"
    wrong = client.post("/admin/create", data={**form, "csrf": "falsch"}, follow_redirects=False)
    assert wrong.status_code == 403

    created = client.post("/admin/create", data={**form, "csrf": csrf_of(client)}, follow_redirects=False)
    assert created.status_code == 303 and created.headers["location"] == "/admin"
    assert "Neuer Tester" in client.get("/admin").text


def test_admin_actions_without_session_are_refused(client):
    response = client.post("/admin/delete", data={"license_key": "ACT-X"}, follow_redirects=False)
    assert response.status_code == 401 and response.json()["error"] == "ADMIN_AUTH_REQUIRED"


def test_bearer_header_works_and_wrong_bearer_is_counted(client):
    assert client.get("/admin", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"}).status_code == 200
    for _ in range(5):
        assert client.get("/admin", headers={"Authorization": "Bearer falsch"}).status_code == 401
    assert client.get("/admin", headers={"Authorization": "Bearer falsch"}).status_code == 429


def test_admin_input_is_validated(client):
    login(client)
    token = csrf_of(client)
    bad_status = client.post(
        "/admin/toggle-status", data={"csrf": token, "license_key": "ACT-X", "new_status": "kaputt"}
    )
    assert bad_status.status_code == 422
    bad_days = client.post(
        "/admin/create",
        data={"csrf": token, "client_name": "Xy", "tool_id": "all", "days_valid": "999999", "max_devices": "1"},
    )
    assert bad_days.status_code == 422


def test_logout_ends_the_session(client):
    login(client)
    token = csrf_of(client)
    assert client.post("/admin/logout", data={"csrf": token}, follow_redirects=False).status_code == 303
    assert client.get("/admin").status_code == 401


# --- Bausteine -----------------------------------------------------------------------------

def test_sliding_window_limiter_with_fake_clock():
    now = [0.0]
    limiter = SlidingWindowLimiter(limit=2, window_seconds=10, clock=lambda: now[0])
    assert limiter.check_and_hit("a") == 0
    assert limiter.check_and_hit("a") == 0
    assert limiter.check_and_hit("a") == 10  # voll: so lange warten
    now[0] = 4
    assert limiter.retry_after("a") == 6
    assert limiter.retry_after("b") == 0
    now[0] = 11
    assert limiter.check_and_hit("a") == 0


def test_admin_sessions_expire():
    now = [0.0]
    sessions = AdminSessions(ttl_seconds=100, clock=lambda: now[0])
    sid = sessions.create()
    assert sessions.csrf_for(sid)
    now[0] = 101
    assert sessions.csrf_for(sid) is None
    assert sessions.csrf_for("unbekannt") is None


def test_client_ip_prefers_last_forwarded_entry():
    assert client_ip({"x-forwarded-for": "1.1.1.1, 2.2.2.2"}, "127.0.0.1") == "2.2.2.2"
    assert client_ip({}, "10.0.0.5") == "10.0.0.5"
    assert client_ip({}, None) == "unknown"
