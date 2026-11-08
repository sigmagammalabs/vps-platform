# VPS Auth- & Licensing-Server (Sigma Gamma Labs)

Zentraler Authentifizierungs- und Hardware-Aktivierungsserver für Desktop-Tools (z. B. **TradingView AI**, **Pre-Market Screener**, **Arbitrage Scout**).

Bindet Lizenzen an die Hardware-ID (Node-Locking) des Ziel-PCs. Wird die Software auf einen anderen Computer kopiert, wird der Start sofort serverseitig mit `HTTP 403 (DEVICE_LIMIT_REACHED)` blockiert.

---

## 1. Übersicht & Architektur

* **Port:** `8100` (im Container und lokal gebunden auf `127.0.0.1:8100`)
* **Tailscale Serve URL:** `https://vps.tail5c3478.ts.net:8100`
* **Admin-Dashboard:** `https://vps.tail5c3478.ts.net:8100/admin`
* **Datenpfad:** `/srv/apps/auth-server/data` (SQLite-Datenbank mit WAL-Modus)
* **UID:** `10001` (Standard-App-Nutzer auf der VPS-Plattform)

---

## 2. Schnelle Inbetriebnahme auf dem VPS

### Schritt 1: In `platform/serve.conf` eintragen
Ergänzen Sie in `platform/serve.conf` (Repo `vps-platform`) die Zeile:
```conf
8100  http://127.0.0.1:8100  AuthServer
```
Danach auf dem VPS ausführen:
```bash
bash /opt/platform/scripts/tailscale-serve.sh apply
```

### Schritt 2: Stack in Portainer anlegen (oder CLI)
1. In Portainer unter **Stacks -> Add stack**.
2. Name: `auth-server`
3. Inhalt der `compose.yaml` einfügen (oder Repo verbinden).
4. Umgebungsvariablen (`.env`) setzen:
   - `AUTH_ADMIN_TOKEN`: Ihr persönliches, geheimes Admin-Passwort.
   - `AUTH_JWT_SECRET`: Zufälliger Salt-String für Signierung.
   - `DATA_ROOT`: `/srv/apps/auth-server`
   - `ALLOWED_HOSTS`: `vps.tail5c3478.ts.net,127.0.0.1,localhost`
5. Auf **Deploy the stack** klicken.

*(Alternativ per Terminal auf dem VPS:)*
```bash
cd /opt/platform/stacks/auth-server
cp .env.example .env
nano .env
docker compose up -d --build
```

---

## 3. Web-Dashboard bedienen

Rufen Sie im Browser auf (sofern Ihr Gerät im Tailnet ist):
👉 **`https://vps.tail5c3478.ts.net:8100/admin`**

* **Admin-Token eingeben**: Entspricht `AUTH_ADMIN_TOKEN`.
* **Lizenz erstellen:** Name eingeben, Tool wählen (`tradingview_ai` oder `all`), Gültigkeit wählen (z. B. 30 Tage oder Unbegrenzt) -> **Lizenz erzeugen**.
* **HW-Reset:** Falls ein Kunde einen neuen PC kauft, klicken Sie auf `🔄 HW-Reset`, damit das neue Gerät gebunden werden kann.
* **Kill-Switch:** Klick auf `🔒 Sperren` entzieht dem Kunden sofort den Zugriff in Echtzeit.

---

## 4. Wichtiger Hinweis zu Testern außerhalb von Tailscale (Tailscale Funnel)

* **Wenn der Tester in Ihrem Tailnet ist:**  
  Das Desktop-Tool erreicht `https://vps.tail5c3478.ts.net:8100` direkt über das verschlüsselte Tailnet.
* **Wenn der Tester ein externer Dritter OHNE Tailscale ist:**  
  Tailscale bietet die geniale Funktion **Tailscale Funnel**. Damit schalten Sie diesen einen Port öffentlich frei, ohne Ports in der Server-Firewall öffnen zu müssen:
  ```bash
  tailscale funnel --bg 8100 on
  ```
  Danach ist `https://vps.tail5c3478.ts.net:8100` auch für externe Tester über das weltweite Internet mit gültigem SSL-Zertifikat erreichbar!

---

## 5. API-Schnittstelle für Desktop-Clients

### `POST /api/v1/verify`
```json
{
  "license_key": "ACT-XXXX-XXXX-XXXX-XXXX",
  "hwid": "TVKI-16D5-A8DD-6D04-E3DE",
  "tool_id": "tradingview_ai",
  "device_name": "DESKTOP-ABC"
}
```

* **Erfolg (200 OK):**
  Liefert `{ "valid": true, "client_name": "...", "expires_at": "...", "days_left": 30, "token": "GRACE-..." }`
* **Weitergabe blockiert (403 Forbidden):**
  Liefert `{ "error": "DEVICE_LIMIT_REACHED", "detail": "Diese Lizenz ist bereits an einen anderen Computer gebunden." }`
