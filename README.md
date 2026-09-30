# VPS-Plattform: Hetzner · Docker · Portainer · Tailscale

Infrastruktur-Repo für den Hetzner-VPS. Enthält alles, um den Server von Grund
auf einzurichten und beliebig viele Apps sauber getrennt als Docker-Stacks zu
betreiben – mit Weboberfläche für Verwaltung und Monitoring, die **nur über
Tailscale** erreichbar ist.

Aktuell laufen zwei Apps:

| App | Repo | Stack | Zeitplan |
|---|---|---|---|
| Arbitrage Selling Scout | [Arbitrage-Scout](https://github.com/sigmagammalabs/Arbitrage-Scout) | `stacks/arbitrage-scout` | täglich 06:15 |
| Pre-Market Screener (Wertpapiere Watchliste) | [Stock-Pre-Market-Screener](https://github.com/sigmagammalabs/Stock-Pre-Market-Screener) | `stacks/premarket-screener` | Mo–Fr 08:15 |

## Aufbau

```
  Laptop / Handy (im Tailnet)
        │  HTTPS, nur für eigene Tailscale-Geräte
        ▼
┌──────────────────────────── Hetzner VPS ─────────────────────────────┐
│  tailscaled ── Tailscale Serve ──► 127.0.0.1:<port>                  │
│                                                                      │
│  Stack "platform"          (scripts/platform.sh)                     │
│    Homepage   :443    Startseite, Status + CPU/RAM je Container      │
│    Portainer  :9443   Container/Stacks verwalten, Apps deployen      │
│    Beszel     :8090   Monitoring mit Verlauf und Alarmen             │
│    Dozzle     :8080   Live-Logs                                      │
│    Ofelia     :8081   Zeitpläne (Cron für Container) + Job-Verlauf   │
│    socket-proxy       Nur-Lese-Docker-API für Homepage/Dozzle        │
│                                                                      │
│  Stack "arbitrage-scout"          Stack "premarket-screener"         │
│    Telegram-Listener               Telegram-Listener                 │
│    + täglicher Lauf (Ofelia)       + Scan Mo–Fr (Ofelia)             │
│    /srv/apps/arbitrage-scout       /srv/apps/premarket-screener      │
│                                                                      │
│  UFW: eingehend nur Tailnet (+ SSH, bis auf Tailnet-only umgestellt) │
└──────────────────────────────────────────────────────────────────────┘
        ▲
        │ docker pull
  GitHub Actions → ghcr.io/sigmagammalabs/<app>   (Images werden in CI gebaut)
```

### Warum so

- **Keine offenen Web-Ports.** Alle Oberflächen binden an `127.0.0.1` und sind
  nur über Tailscale Serve erreichbar (echtes HTTPS-Zertifikat, nur eigene
  Geräte). Wichtig, weil Docker veröffentlichte Ports an UFW vorbei öffnet – ein
  `-p 9443:9443` wäre trotz Firewall aus dem Internet erreichbar.
- **Jede App ist ein eigener Stack** mit eigenem Netzwerk, eigenem
  Datenverzeichnis unter `/srv/apps/<app>`, eigenen Secrets und
  Ressourcengrenzen. Apps sehen sich gegenseitig nicht.
- **Der VPS baut nichts.** GitHub Actions testet und baut die Images
  (amd64 + arm64) und legt sie in der GitHub Container Registry ab. Der Server
  zieht nur fertige Images; ein Rollback ist ein anderer Image-Tag.
- **Zeitpläne stehen an der App**, nicht auf dem Host: als Ofelia-Labels im
  Stack. Ofelia startet den Lauf per `docker exec` *im laufenden App-Container*.
  Das ist beim Scout zwingend: dessen Listener erkennt laufende Scans über die
  PID im Lockfile und stoppt sie per `/stop` – das klappt nur im selben
  PID-Namespace. Ein separater Cron-Container würde Überlappungsschutz und
  `/stop` aushebeln.
- **Skalierbar durch Konvention.** Neue App = Ordner in `stacks/` + Stack in
  Portainer. Startseite, Logs, Monitoring und Zeitplan greifen automatisch über
  Labels (→ [docs/neue-app.md](docs/neue-app.md)).

## Verzeichnisse

```
Repo (auf dem VPS unter /opt/platform)
├── bootstrap/host-setup.sh      Server-Grundeinrichtung (einmalig, idempotent)
├── platform/
│   ├── compose.yaml             Portainer, Homepage, Beszel, Dozzle, Ofelia
│   ├── .env.example             Plattform-Einstellungen (→ platform/.env)
│   ├── serve.conf               welche Oberfläche unter welchem Tailnet-Port
│   ├── logrotate.conf           Rotation der App-Logdateien (→ /etc/logrotate.d)
│   └── homepage/                Startseiten-Konfiguration
├── stacks/
│   ├── arbitrage-scout/         compose.yaml + .env.example (Variablennamen)
│   ├── premarket-screener/
│   └── _template/               Vorlage für neue Apps
├── scripts/
│   ├── platform.sh              up | update | status | logs | down
│   ├── tailscale-serve.sh       Tailnet-Freigaben aus serve.conf setzen
│   └── backup.sh                Portainer, Beszel, /srv/apps sichern
└── docs/
    ├── betrieb.md               Alltag, Updates, Backups, Fehlersuche
    └── neue-app.md              weitere Container hinzufügen

Auf dem VPS zusätzlich
/srv/apps/<app>/                 Daten + Logs je App (Bind-Mounts)
Docker-Volumes                   portainer_data, beszel_data, ...
```

## Einrichtung Schritt für Schritt

### 0. Voraussetzungen

- Hetzner Cloud Server mit **Ubuntu 24.04**, beim Anlegen einen SSH-Key
  hinterlegt. Größe: ab CX22/CAX11 (2 vCPU, 4 GB) reicht für Plattform + beide Apps.
- Tailscale-Konto; der eigene Rechner ist bereits im Tailnet.
- Zugriff auf die GitHub-Organisation `sigmagammalabs`.

### 1. Code nach GitHub bringen (lokal)

**App-Repos** – in beiden Repos sind neue Dateien hinzugekommen:

| Repo | Neu / geändert |
|---|---|
| Arbitrage Selling Scout | `.github/workflows/docker-publish.yml` (Tests + Image), `Dockerfile` (tzdata, damit `TZ=Europe/Berlin` wirkt) |
| Wertpapiere Watchliste | `Dockerfile`, `.dockerignore`, `.github/workflows/docker-publish.yml` |

Committen und pushen. Danach unter *Actions* prüfen, dass der Workflow
„Docker-Image" grün durchläuft. Das erzeugt
`ghcr.io/sigmagammalabs/arbitrage-scout:latest` und
`ghcr.io/sigmagammalabs/premarket-screener:latest`.

**Images abrufbar machen** – neue Pakete in GHCR sind zunächst privat. Entweder
unter *GitHub → Organisation → Packages → Paket → Package settings → Change
visibility* auf **Public** stellen (die Repos sind ohnehin öffentlich), oder in
Schritt 6 eine Registry mit Token in Portainer hinterlegen.

**Dieses Repo** als eigenes GitHub-Repo anlegen, z. B.
`sigmagammalabs/vps-platform`, und pushen. Es enthält keine Secrets.
Öffentlich ist am einfachsten; privat geht auch (dann braucht der VPS und
Portainer einen Token mit Leserecht).

### 2. Hetzner vorbereiten (Cloud Console)

- **Firewall** anlegen und dem Server zuweisen – eingehend nur:
  TCP 22 (SSH, bis Schritt 9) und UDP 41641 (Tailscale-Direktverbindung).
  Diese Firewall sitzt vor dem Server und lässt sich von Docker nicht umgehen.
- **Backups** für den Server aktivieren (tägliche Snapshots, 7 Stück).

### 3. Server einrichten

```bash
ssh root@<öffentliche-IP>
```

```bash
apt-get update && apt-get install -y git
git clone https://github.com/sigmagammalabs/vps-platform.git /opt/platform
cd /opt/platform
bash bootstrap/host-setup.sh
```

Das Skript installiert Updates, Docker, Tailscale, Firewall und härtet SSH.
Bei der Tailscale-Anmeldung erscheint ein Link – im Browser öffnen und den
Server freigeben. Optionen (`--help`): `--admin-user NAME` legt einen
sudo-Benutzer an und schaltet root-Login ab, `--ts-hostname` ändert den Namen
im Tailnet (Standard `vps`).

Meldet das Skript am Ende einen ausstehenden Neustart: `reboot`, danach wieder
einloggen.

### 4. Tailscale-Adminkonsole

- *DNS*: **MagicDNS** und **HTTPS Certificates** aktivieren.
- *Machines → vps → ⋯*: **Disable key expiry**. Sonst fällt der Server nach
  180 Tagen aus dem Tailnet und alle Oberflächen sind weg.

### 5. Plattform starten

```bash
cd /opt/platform
bash scripts/platform.sh up
```

Legt `platform/.env` an (trägt den Tailnet-Namen selbst ein), startet alle
Dienste und richtet die HTTPS-Freigaben ein. Die URLs stehen am Ende der
Ausgabe, z. B. `https://vps.tail1234.ts.net`.

**Sofort Portainer öffnen** (`https://vps.<tailnet>.ts.net:9443`) und das
Admin-Konto anlegen – nach 5 Minuten sperrt Portainer die Ersteinrichtung
(dann `docker restart portainer`). Portainer fragt dabei nach einem
**Setup-Token**; es steht im Log (nach jedem Neustart ein neues):

```bash
docker logs portainer 2>&1 | grep setup_token | tail -1
```

Als Umgebung „Get Started" → *local*.

Adressen im Browser immer **mit `https://`** eingeben – ohne Schema nimmt
Chrome bei Adressen mit Port `http://`, und Tailscale antwortet dann mit
„Client sent an HTTP request to an HTTPS server".

Danach Beszel einrichten (Admin-Konto, Agent verbinden):
[docs/betrieb.md → Monitoring](docs/betrieb.md#monitoring-mit-beszel).

### 6. Apps in Portainer deployen

Nur falls die GHCR-Pakete privat sind: *Registries → Add registry → GitHub*
(Benutzer + Personal Access Token mit `read:packages`).

Für jede App: *Stacks → Add stack*

| Feld | Arbitrage Scout | Pre-Market Screener |
|---|---|---|
| Name | `arbitrage-scout` | `premarket-screener` |
| Build method | Repository | Repository |
| Repository URL | `https://github.com/sigmagammalabs/vps-platform` | ← gleich |
| Repository reference | `refs/heads/main` | ← gleich |
| Compose path | `stacks/arbitrage-scout/compose.yaml` | `stacks/premarket-screener/compose.yaml` |
| Environment variables | *Advanced mode* → Inhalt von `stacks/arbitrage-scout/.env.example` einfügen und ausfüllen | ← entsprechend |
| GitOps updates | aus | aus |

*Deploy the stack*. Kurz darauf erscheinen beide Apps auf der Startseite, ihre
Zeitpläne in Ofelia.

Hinweise zu Portainer 2.45:

- Die URL ohne Leerzeichen davor einfügen – sonst meldet Portainer nur
  „Unable to test the connection“. Beim ersten Stack legt Portainer das Repo
  automatisch als *Source* an (*App Delivery → Sources*), weitere Stacks können
  sie auswählen.
- Umgebungsvariablen eines bestehenden Stacks stehen unter
  **Edit stack settings**; wirksam werden sie erst mit **Pull and redeploy**.
- Automatische GitOps-Updates aus lassen: in 2.45 legt ein manuelles
  *Pull and redeploy* sie dauerhaft lahm
  ([portainer#13298](https://github.com/portainer/portainer/issues/13298)).
  Updates daher von Hand, siehe [docs/betrieb.md](docs/betrieb.md#neue-version-ausrollen).
- Jede App braucht ihren **eigenen Telegram-Bot**: zwei Listener mit demselben
  Token nehmen sich gegenseitig die Nachrichten weg. Die Chat-ID ist die eigene
  Telegram-ID (reine Zahl, z. B. über @userinfobot) – nicht der Bot-Name.

### 7. Scout-Einkaufsliste übernehmen

Auf dem Server läuft der Scout im Modus `api` (Stack-Variable
`SCOUT__SOURCES__PROVIDER`): Er liest die Einkaufsliste
`data/purchases.csv` und sucht passende eBay-Angebote selbst. Die Liste ist
nicht im Repo und damit auch nicht im Image – ohne sie bricht der tägliche Lauf
mit „Einkaufsliste nicht gefunden“ ab. Spalten wie in
`data/purchases.example.csv`; Pflicht sind `title` und `price_eur`, eine `ean`
macht die eBay-Suche deutlich präziser. Vom eigenen Rechner (PowerShell)
kopieren:

```powershell
scp "F:\AI-CODE-AREA\Arbitrage Selling Scout\data\purchases.csv" root@vps:/srv/apps/arbitrage-scout/data/
```

```bash
ssh root@vps chown app:app /srv/apps/arbitrage-scout/data/purchases.csv
```

Mit `--admin-user` ist root-Login gesperrt: dann nach `/tmp` kopieren und auf
dem Server per `sudo mv` + `sudo chown` an die Stelle bringen.

### 8. Testen

- Startseite: beide Apps grün, CPU/RAM sichtbar.
- Telegram: `/status` an den Scout-Bot, `/help` an den Screener-Bot.
- Ofelia-Oberfläche: Jobs `arbitrage-scout-daily` und `premarket-screener-scan`
  mit nächster Ausführungszeit.
- Prüfen ohne API-Kosten, auf dem Server:

  ```bash
  docker exec -u 10001 arbitrage-scout python scout.py --check-config
  docker exec -u 10001 premarket-screener python main.py scan --universe custom --tickers SAP.DE,SIE.DE --min-gap-pct 0.1
  ```

### 9. SSH nur noch über Tailscale

In einem **zweiten** Terminal testen, dass SSH über das Tailnet klappt
(mit `--admin-user` statt `root` dessen Namen verwenden):

```bash
ssh root@vps
```

Dann auf dem Server:

```bash
bash /opt/platform/bootstrap/host-setup.sh --ssh-tailscale-only
```

und in der Hetzner-Firewall die Regel für TCP 22 löschen. Notfallzugang bleibt
die Web-Konsole in der Hetzner Cloud Console. Steht in einer SSH-Config die
öffentliche IP als `HostName`, auf den Tailnet-Namen `vps` umstellen.

## Zugriff von jedem Gerät

Alles läuft über das Tailnet – ein Gerät, das nicht darin angemeldet ist,
erreicht weder die Oberflächen noch SSH.

**Weboberflächen** (PC, Laptop, Handy):

1. Tailscale installieren: <https://tailscale.com/download>
2. Mit **demselben Konto** anmelden wie der VPS.
3. `https://<fqdn>` öffnen und als Lesezeichen speichern – von dort führen die
   Kacheln zu Portainer, Beszel, Dozzle und Ofelia. `<fqdn>` steht in
   `platform/.env` auf dem Server bzw. in der Tailscale-Adminkonsole beim
   Gerät `vps` (Form `vps.tailXXXX.ts.net`).

Eigene Logins haben nur Portainer und Beszel (Passwortmanager), Homepage,
Dozzle und Ofelia schützt allein das Tailnet. Geht ein Gerät verloren: in der
Tailscale-Adminkonsole unter *Machines* entfernen – damit ist es sofort
ausgesperrt. Fremde Rechner ohne Tailscale (z. B. Arbeits-PC ohne
Installationsrechte) haben bewusst keinen Zugang; dafür das Handy nehmen.

**SSH** (nur für Wartung nötig – Container-Konsole und Logs gibt es auch in
Portainer): pro Gerät einen eigenen Schlüssel anlegen, statt einen privaten
Schlüssel herumzukopieren. Auf dem neuen Gerät:

```powershell
ssh-keygen -t ed25519 -C "<geraetename> vps"
```

Den Inhalt der erzeugten `.pub`-Datei von einem Gerät mit Zugang aus an
`/root/.ssh/authorized_keys` auf dem VPS anhängen. Danach auf dem neuen Gerät
`ssh root@vps` (MagicDNS-Kurzname). Einen Schlüssel sperren = seine Zeile aus
`authorized_keys` löschen.

## Alte Deploy-Skripte der Apps

Beide App-Repos bringen noch ihre systemd/cron-Einrichtung mit
(`deploy/vps_setup.sh`, `install_cron.sh`, `install_listener_service.sh`). Auf
diesem Server werden sie **nicht** verwendet – parallel betrieben würden Scans
doppelt laufen und zwei Listener um dieselben Telegram-Updates konkurrieren.

## Weiter

- [docs/betrieb.md](docs/betrieb.md) – Updates, Logs, Zeitpläne, Backups, Fehlersuche
- [docs/neue-app.md](docs/neue-app.md) – weitere Container hinzufügen
