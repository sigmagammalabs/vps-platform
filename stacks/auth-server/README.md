# VPS Auth- & Licensing-Server (Sigma Gamma Labs)

Zentraler Lizenzserver für Desktop-Tools (z. B. **TradingView AI**). Eine Lizenz wird beim
ersten Start an die Hardware-ID des PCs gebunden (Node-Locking). Der Server beantwortet jede
Prüfung mit einem **Ed25519-signierten Token**; der Client kennt nur den öffentlichen Schlüssel,
in der Exe steckt kein Geheimnis.

## 1. Überblick

| | |
|---|---|
| Container | `auth-server` (Python 3.12, UID 10001, read-only Dateisystem, Healthcheck) |
| Port | `127.0.0.1:8100`, von außen nur über Tailscale |
| Tailnet | `https://vps.tail5c3478.ts.net:8100` (alles inkl. `/admin`) |
| Öffentlich | nur `/api` über Tailscale Funnel auf Port 10000, **erst nach Freigabe** (Abschnitt 4) |
| Daten | `/srv/apps/auth-server/data`: `licenses.db` und **`signing_key.pem`** |

## 2. Server-Vertrag v2 (verbindlich für Server und Clients)

`POST /api/v1/verify`, JSON:

```json
{ "license_key": "ACT-XXXX-XXXX-XXXX-XXXX", "hwid": "TVKI-XXXX-XXXX-XXXX-XXXX",
  "tool_id": "tradingview_ai", "device_name": "PC-NAME", "nonce": "<16 Zufallsbytes, base64url>" }
```

**200**: `valid`, `client_name`, `tool_id`, `expires_at`, `days_left`, `message` und `token`.

`token = <payload_b64url>.<signature_b64url>`, Signatur Ed25519 über die ASCII-Bytes von
`payload_b64url`. Payload: `v=1`, `hwid`, `tool`, `nonce` (Echo), `iat`, `exp`, `lic_exp`
(Unix-Sekunden, `lic_exp=0` = unbegrenzt), `exp = min(iat + 48 h, lic_exp)`.

**Fehler**: JSON `{"error": CODE, "detail": Text}`

| Status | `error` |
|---|---|
| 404 | `INVALID_KEY` |
| 403 | `LICENSE_SUSPENDED`, `LICENSE_EXPIRED`, `WRONG_TOOL`, `DEVICE_LIMIT_REACHED` |
| 422 | `BAD_REQUEST` (`detail` ist eine Liste aus `loc`/`msg`/`type`, ohne Echo der Eingabe) |
| 429 | `RATE_LIMITED` mit Header `Retry-After` (Sekunden) |

Definitiv abgelehnt wird nur mit **404/403 und einem dieser Codes**. 429, 5xx und Netzwerkfehler
sind keine Ablehnung (Client behält sein Token bis `exp`).

Weitere Endpunkte: `GET /api/health`, `GET /api/v1/public-key` (öffentlicher Schlüssel, Base64
der 32 Rohbytes, zum Eintragen in den Client).

**Anfragebremse** je Aufrufer-Adresse: 30 Anfragen/min, 10 unbekannte Schlüssel je 10 min,
5 falsche Admin-Anmeldungen je 5 min. Die Adresse ist der letzte `X-Forwarded-For`-Eintrag;
Tailscale Serve überschreibt dort Werte des Aufrufers (getestet).

## 3. Installation (auf dem VPS)

```bash
cd /opt/platform/stacks/auth-server
cp .env.example .env && chmod 600 .env
# AUTH_ADMIN_TOKEN eintragen (mind. 24 Zeichen, zufällig):
#   openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | cut -c1-40
docker compose up -d --build
tailscale serve --bg --https=8100 http://127.0.0.1:8100   # oder: scripts/tailscale-serve.sh apply
```

Ohne `AUTH_ADMIN_TOKEN` (oder mit einem bekannten Standardwert) startet der Server nicht. Beim
allerersten Start erzeugt er `data/signing_key.pem`; der öffentliche Schlüssel steht im Log
(`docker logs auth-server`) und unter `/api/v1/public-key`.

## 4. Öffentlicher Zugang für Tester ohne Tailscale (Funnel, nur `/api`)

Funnel gibt einen Pfad ins Internet frei, ohne dass der Tester Tailscale braucht. **Nur `/api`**
wird durchgereicht; `/admin`, `/docs` und alles andere beantwortet Tailscale selbst mit 404.

1. Einmalig in der Tailscale-Adminkonsole Funnel für das Tailnet erlauben (der Befehl unten
   zeigt dazu beim ersten Aufruf einen Link).
2. In `platform/serve.conf` die Zeile `funnel 10000 /api ...` einkommentieren und
   `bash scripts/tailscale-serve.sh apply` ausführen. Die Zeile gehört in die Datei, weil der
   Reset in diesem Skript sonst jeden von Hand gesetzten Funnel entfernt.
3. Prüfen, **außerhalb** des Tailnets (Handy im Mobilfunk):
   `https://vps.tail5c3478.ts.net:10000/api/health` muss antworten, `.../admin` muss 404 liefern.

Zurücknehmen: Zeile wieder auskommentieren und `apply`, oder `tailscale funnel --https=10000 off`.

## 5. Admin-Dashboard

`https://vps.tail5c3478.ts.net:8100/admin`, Anmeldung mit `AUTH_ADMIN_TOKEN`. Danach hängt die
Sitzung an einem Cookie (8 h, `HttpOnly`, `SameSite=Strict`); das Token steht nie in der URL.
Formulare tragen ein CSRF-Feld. Für Skripte geht weiterhin `Authorization: Bearer <Token>`.

* **Lizenz anlegen:** Name, **Programm**, Gültigkeit, Geräte-Limit. Das Programm ist ein
  Pflichtfeld **ohne Vorauswahl**; ein Schlüssel gilt nur für das gewählte Programm (oder für
  alle, bei „Alle Tools“). Der Name erscheint im Programm, im BCS-Cockpit als aktiver Bearbeiter.
* **Filter:** Über der Tabelle zeigt je Programm ein Knopf die Zahl der Lizenzen; ein Klick
  filtert die Tabelle (`/admin?tool=telefon_akquise`).
* **HW-Reset:** gibt die gebundenen PCs frei (neuer Rechner des Kunden).
* **Sperren:** wirkt beim nächsten Abgleich des Clients (spätestens nach 24 h online).

## 6. Programme (mehrere Tools über einen Server)

Welche Programme es gibt, steht **nur** in `app/tools.py` (`TOOLS`: Kennung → Bezeichnung).
Daraus entstehen Formular, Filter, Eingabeprüfung und Verifikation.

| Kennung (`tool_id`) | Programm |
|---|---|
| `tradingview_ai` | TradingView AI |
| `telefon_akquise` | BCS Telefon-Akquise & Lead-Cockpit |
| `premarket-screener` | Pre-Market Screener |
| `arbitrage-scout` | Arbitrage Scout |
| `all` | Universal-Lizenz: gilt für jedes Programm (nur als Lizenz-Typ) |

Regeln des Servers:

* Ein Programm meldet sich mit **seiner eigenen** Kennung. Passt sie nicht zur Lizenz, kommt
  `403 WRONG_TOOL`: Ein TradingView-Schlüssel öffnet kein BCS und umgekehrt.
* Unbekannte Kennungen und `all` als angefragtes Programm werden mit `WRONG_TOOL` abgewiesen,
  noch bevor der Schlüssel geprüft wird (zählt nicht als Raten).
* Das signierte Token nennt das **angefragte** Programm (`tool`); der Client prüft es auf
  genau seine Kennung. Ein Client darf deshalb **keine** weiteren Kennungen akzeptieren und
  bei `WRONG_TOOL` **nicht** mit einer anderen Kennung erneut anfragen.

**Neues Programm anbinden:** Eintrag in `TOOLS` ergänzen, Server neu starten
(`docker compose up -d --build`), im Client dieselbe Kennung als feste Konstante eintragen
(keine Umgebungsvariable). Danach erscheint es im Dashboard.

## 7. Sicherung und Betrieb

* **`signing_key.pem` ist unersetzlich.** Geht er verloren, prüft kein ausgelieferter Client mehr
  erfolgreich; alle Tester brauchen eine neue Exe mit neuem öffentlichen Schlüssel. Die nächtliche
  Sicherung (`scripts/backup.sh`) enthält `/srv/apps`; zusätzlich eine Kopie offline ablegen:
  `scp vps:/srv/apps/auth-server/data/signing_key.pem <sicherer Ort>`.
* **Admin-Token wechseln:** in `.env` ersetzen, `docker compose up -d` (Sitzungen enden dabei).
* **Tests:** `pip install -r requirements-dev.txt && python -m pytest` (45 Tests, ohne Netzwerk).
* **Audit-Log:** wird auf die letzten 50.000 Einträge begrenzt.
