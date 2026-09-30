# Betrieb

Alle Befehle auf dem Server als root (bzw. mit `sudo`) im Verzeichnis
`/opt/platform`. `<fqdn>` steht für den Tailnet-Namen, z. B. `vps.tail1234.ts.net`
(steht in `platform/.env`).

| Oberfläche | URL | Wofür |
|---|---|---|
| Homepage | `https://<fqdn>` | Überblick: Status, CPU/RAM je Container, Links |
| Portainer | `https://<fqdn>:9443` | Stacks deployen/aktualisieren, Container neu starten, Konsole |
| Beszel | `https://<fqdn>:8090` | Verlauf CPU/RAM/Disk/Netz je Container, Alarme |
| Dozzle | `https://<fqdn>:8080` | Live-Logs aller Container, Suche |
| Ofelia | `https://<fqdn>:8081` | Zeitpläne, letzte Läufe mit Ausgabe |

## Apps

### Neue Version ausrollen

1. Änderung im App-Repo nach `main` pushen → GitHub Actions baut
   `ghcr.io/sigmagammalabs/<app>:latest` (beim Scout erst nach grünen Tests).
2. Portainer → *Stacks → <app>* → **Pull and redeploy**, dabei
   **Re-pull image** aktivieren.

Der Container wird dabei neu erstellt. Ein gerade laufender Scan bricht ab –
also nicht kurz vor/während eines geplanten Laufs ausrollen.

**Rollback:** In den Stack-Variablen `IMAGE_TAG=sha-<commit>` setzen (die
Tags stehen im Repo unter *Packages*), *Pull and redeploy*. Zurück mit
`IMAGE_TAG=latest`.

### Secrets und Einstellungen ändern

Portainer → *Stacks → <app>* → Abschnitt *Environment variables* anpassen →
*Pull and redeploy*. Welche Variablen es gibt, steht in
`stacks/<app>/.env.example`.

Portainer speichert diese Werte in seiner Datenbank (Volume `portainer_data`)
– das ist der Grund, warum `scripts/backup.sh` dieses Volume sichert.

### Zeitpläne

Stehen als Labels in `stacks/<app>/compose.yaml`, die Uhrzeit ist über eine
Variable überschreibbar (`SCOUT_SCHEDULE`, `SCREENER_SCHEDULE`, Cron-Syntax mit
5 Feldern, Zeitzone `Europe/Berlin`). Nach der Änderung *Pull and redeploy* –
Ofelia übernimmt neue Labels ohne Neustart.

Job sofort ausführen (auf dem Server):

```bash
docker exec -u 10001 arbitrage-scout python scout.py --notify-telegram
docker exec -u 10001 premarket-screener python main.py scan --universe eurostoxx50 --export csv --notify-telegram --ai-briefing
```

`-u 10001` ist wichtig: sonst laufen die Befehle als anderer Benutzer, und
Dateien in `/srv/apps` gehören danach nicht mehr der App.

### Arbitrage Scout: Daten und Konfiguration

- **Angebotslisten:** `/srv/apps/arbitrage-scout/data/offers.csv` bzw.
  `purchases.csv`. Nach dem Bearbeiten als root:
  `chown 10001:10001 /srv/apps/arbitrage-scout/data/*.csv`.
  Bequem geht das auch mit WinSCP oder VS Code Remote-SSH über das Tailnet.
- **Ergebnisse und Logs:** `/srv/apps/arbitrage-scout/data` und `.../logs`
  (`scout.log` rotiert selbst).
- **Eigene `config.yaml`** (Suchbegriffe, Schwellen), ohne neues Image:

  ```bash
  docker cp arbitrage-scout:/app/config.yaml /srv/apps/arbitrage-scout/config/config.yaml
  nano /srv/apps/arbitrage-scout/config/config.yaml
  ```

  In Portainer `SCOUT_CONFIG_FILE=/app/userconfig/config.yaml` setzen,
  *Pull and redeploy*. Prüfen:
  `docker exec -u 10001 arbitrage-scout python scout.py --check-config`.

### Pre-Market Screener

- **Exporte und Log:** `/srv/apps/premarket-screener/watchlist/`
  (`watchlist_*.csv`, `screener.log`).
- **Universum** für Listener und Scan: Variable `SCREENER_UNIVERSE`
  (`eurostoxx50`, `dax40`, `sp500`, `nasdaq100`). Bei US-Werten auch
  `SCREENER_SCHEDULE` anpassen (US-Pre-Market liegt nachmittags Berliner Zeit).

## Logs

- **Dozzle** – alles, was die Container auf stdout/stderr schreiben, live und
  durchsuchbar. Docker rotiert diese Logs (3 × 10 MB pro Container).
- **Ofelia** – Ausgabe jedes geplanten Laufs im Job-Verlauf.
- **Dateien** – `/srv/apps/<app>/...`. Beachten: Scans, die der Scout-Listener
  per `/scan` startet, schreiben nur in `logs/scout.log`, nicht nach stdout.

## Monitoring mit Beszel

Einmalig einrichten:

1. `https://<fqdn>:8090` öffnen, Admin-Konto anlegen.
2. *Add System*: Name `vps`, Host/IP **`/beszel_socket/beszel.sock`**
   (lokaler Agent über Unix-Socket, kein Port).
3. Beszel zeigt **Public Key** und **Token**. Beide in `platform/.env`
   eintragen – den Key in Anführungszeichen, er enthält ein Leerzeichen:

   ```
   BESZEL_AGENT_KEY="ssh-ed25519 AAAA..."
   BESZEL_AGENT_TOKEN=...
   ```

4. `bash scripts/platform.sh up` – der Agent startet jetzt mit.

**Alarme per Telegram:** *Settings → Notifications* → URL im Shoutrrr-Format
`telegram://<BOT_TOKEN>@telegram?chats=<CHAT_ID>` (eigener Bot empfohlen,
damit sich Alarme und App-Meldungen nicht mischen). Danach am System die
Alarme aktivieren, sinnvoll sind: *Status* (Server weg), *Disk* > 80 %,
*Memory* > 90 %, *CPU* > 90 % über 10 Minuten.

## Plattform aktualisieren

```bash
git pull                              # Änderungen an diesem Repo
bash scripts/platform.sh up           # Konfiguration anwenden
bash scripts/platform.sh update       # neue Versionen von Portainer, Beszel & Co.
```

Die Plattform-Images laufen auf `latest` bzw. `lts` (Portainer) und ändern
sich nur, wenn `update` ausgeführt wird. Vorher lohnt `scripts/backup.sh`.

Das Betriebssystem spielt Sicherheitsupdates selbst ein. Liegt
`/var/run/reboot-required` vor (Kernel-Update), zu einer ruhigen Zeit
`reboot` – alle Container starten danach von selbst.

## Backups

Zwei Ebenen:

1. **Hetzner-Backups** (Cloud Console): tägliche Snapshots des ganzen
   Servers, 7 Stück. Schutz gegen Totalverlust.
2. **`scripts/backup.sh`**: sichert Portainer (inkl. aller Stack-Secrets),
   Beszel, `/srv/apps` und `platform/.env` nach `/var/backups/platform`
   (14 Tage). Gut vor Updates und für das gezielte Zurückholen einzelner
   Dateien.

Täglich automatisch, z. B. 03:30:

```bash
echo '30 3 * * * root bash /opt/platform/scripts/backup.sh >> /var/log/platform-backup.log 2>&1' > /etc/cron.d/platform-backup
```

Portainer und Beszel sind während der Sicherung wenige Sekunden aus; die Apps
laufen weiter.

Wiederherstellen eines Volumes (Beispiel Portainer):

```bash
docker stop portainer
docker run --rm -v portainer_data:/data -v /var/backups/platform:/backup busybox:stable \
  sh -c 'rm -rf /data/* && tar xzf /backup/portainer_data_<DATUM>.tar.gz -C /data'
docker start portainer
```

## Fehlersuche

| Symptom | Ursache / Lösung |
|---|---|
| Portainer: „timed out for security purposes" | Admin-Konto nicht innerhalb von 5 Min. angelegt → `docker restart portainer`, dann sofort anlegen |
| Portainer fragt nach „Setup token" | `docker logs portainer 2>&1 \| grep setup_token \| tail -1` – nach jedem Neustart ein neues |
| „Client sent an HTTP request to an HTTPS server" | Adresse mit `https://` eingeben (Chrome nimmt bei Adressen mit Port sonst `http://`) |
| Portainer: „Forbidden – origin invalid" | In `platform/compose.yaml` bei Portainer `TRUSTED_ORIGINS` einkommentieren (Format je nach Version, steht dort), `scripts/platform.sh up` |
| Oberflächen nicht erreichbar | `tailscale status` (Gerät online?), `bash scripts/tailscale-serve.sh status`, in der Adminkonsole MagicDNS + HTTPS Certificates aktiv? |
| `tailscale serve` wartet mit Link | HTTPS im Tailnet noch nicht freigeschaltet – Link öffnen |
| Stack-Deploy: „pull access denied" / „denied" | GHCR-Paket privat → öffentlich stellen oder Registry in Portainer hinterlegen |
| App-Container startet ständig neu | Logs in Dozzle; oft fehlende/fehlerhafte Variable. `docker inspect <container> --format '{{.State.OOMKilled}}'` = `true` → `MEM_LIMIT` erhöhen |
| „Listener aus, Container bleibt … aktiv" im Log | `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` fehlen – gewollt, geplante Läufe funktionieren trotzdem |
| Job erscheint nicht in Ofelia | Label `ofelia.enabled: "true"` am Container? `docker logs ofelia` zeigt abgelehnte Zeitpläne |
| Scout: `Permission denied` in `/app/data` | Dateien gehören nicht UID 10001 → `chown -R 10001:10001 /srv/apps/arbitrage-scout/data` |
| Scout-Lauf scheitert an fehlender CSV | `offers.csv`/`purchases.csv` fehlt in `/srv/apps/arbitrage-scout/data` (README, Schritt 7) |
| Server nach 180 Tagen nicht mehr im Tailnet | Key abgelaufen → per Hetzner-Web-Konsole `tailscale up`, danach *Disable key expiry* |

## Sicherheit – was bewusst so ist

- **Docker-Socket:** Portainer, Ofelia und der Beszel-Agent haben vollen
  Zugriff auf die Docker-API (entspricht root). Homepage und Dozzle bekommen
  nur den Nur-Lese-Proxy. Deshalb laufen nur bekannte, gepflegte Images mit
  Socket-Zugriff – neue Dienste nicht leichtfertig an den Socket hängen.
- **Kein Login vor Homepage, Dozzle, Ofelia:** Schutz ist das Tailnet. Wer
  mehr Personen ins Tailnet holt, sollte per Tailscale-ACL den Zugriff auf den
  VPS auf die eigenen Geräte beschränken.
- **Tailscale Funnel** (Freigabe ins öffentliche Internet) wird nie benutzt.
