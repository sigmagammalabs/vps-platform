# Neue App hinzufügen

Jede App bekommt einen eigenen Stack – eigenes Netzwerk, eigenes
Datenverzeichnis, eigene Secrets, eigene Ressourcengrenzen. Startseite, Logs,
Monitoring und Zeitplan greifen automatisch, sobald die Konventionen unten
eingehalten sind.

## 1. App-Repo: Image bauen lassen

**Dockerfile** – am einfachsten vom Screener abschauen
(`Wertpapiere Watchliste/Dockerfile`). Wichtig:

- eigener Benutzer mit **UID 10001** (`useradd --uid 10001 app`, `USER app`) –
  darauf verlassen sich `init-dirs` und die Ofelia-Jobs
- bei `python:*-slim`: `tzdata` installieren, sonst gilt `TZ` nicht
- Logs nach stdout/stderr (dann sieht Dozzle sie)
- keine Secrets im Image, `.env` in `.dockerignore`

**Workflow** – `.github/workflows/docker-publish.yml` aus einem der beiden
App-Repos kopieren und nur den Image-Namen in `env.IMAGE` ändern. Push nach
`main` → `ghcr.io/sigmagammalabs/<app>:latest`. Paket in GitHub auf *Public*
stellen (oder Registry in Portainer hinterlegen).

## 2. Stack in diesem Repo anlegen

```bash
cp -r stacks/_template stacks/<app>
```

In `stacks/<app>/compose.yaml` alle `myapp` ersetzen und anpassen:

| Stelle | Was |
|---|---|
| `name:` | Stack-Name, gleich wie später in Portainer |
| `image:` | `ghcr.io/sigmagammalabs/<app>` |
| `container_name:` | eindeutig auf dem Server |
| `init-dirs` | alle Verzeichnisse anlegen, in die die App schreibt |
| `environment:` | jede Variable als `NAME: ${NAME:-}` – nur so kommt sie aus Portainer in den Container |
| `volumes:` | nur unter `/srv/apps/<app>/` |
| `mem_limit`, `cpus` | realistische Obergrenze |
| Ofelia-Labels | nur bei Zeitplänen; Job-Namen serverweit eindeutig, `user: "10001"` nicht vergessen (Ofelia-Standard ist `nobody`) |
| Homepage-Labels | Kachel auf der Startseite (`homepage.group: Apps`) |

`stacks/<app>/.env.example` mit allen Variablennamen füllen – Werte nie ins
Repo.

**Reine Cron-App ohne Dauerprozess:** `command: ["sleep", "infinity"]` – der
Container dient dann nur als Ziel für den geplanten `docker exec`. Vorteil
gegenüber einem Wegwerf-Container pro Lauf: gleiche Umgebung, Secrets und
Mounts, sichtbar in allen Oberflächen.

Committen, pushen.

## 3. In Portainer deployen

*Stacks → Add stack → Repository*, wie im README (Schritt 6) – nur mit
Compose path `stacks/<app>/compose.yaml` und den Variablen aus
`stacks/<app>/.env.example`.

## 4. Nur wenn die App eine Weboberfläche hat

1. In der Compose-Datei an `127.0.0.1` binden, nie an alle Interfaces:

   ```yaml
   ports:
     - "127.0.0.1:8100:8000"
   ```

2. Zeile in `platform/serve.conf`:

   ```
   8100  http://127.0.0.1:8100   MeineApp
   ```

3. Auf dem Server `git pull && bash scripts/tailscale-serve.sh apply`.
4. Label `homepage.href: https://<fqdn>:8100` ergänzen, damit die Kachel
   verlinkt.

Freie Ports im Blick behalten: 3000, 8080, 8081, 8090, 9443 belegt die
Plattform. Vorschlag: Apps ab 8100 aufwärts.

## Checkliste

- [ ] Image läuft als UID 10001, `tzdata` installiert
- [ ] Workflow grün, Paket abrufbar
- [ ] `stacks/<app>/compose.yaml`: Name, Image, Container-Name, Volumes unter `/srv/apps/<app>`
- [ ] alle Secrets als `${NAME:-}` durchgereicht, `.env.example` aktuell
- [ ] Ressourcengrenzen gesetzt
- [ ] Ofelia-Job mit eindeutigem Namen und `user: "10001"`
- [ ] Homepage-Labels
- [ ] Ports nur an 127.0.0.1, ggf. `serve.conf` ergänzt

## Wenn ein Server nicht mehr reicht

- **Größer machen:** Hetzner *Rescale* (CPU/RAM hoch, Disk optional) – kurzer
  Neustart, sonst ändert sich nichts.
- **Zweiter Server:** dort nur `bootstrap/host-setup.sh` und den
  Portainer-Agent + Beszel-Agent starten. Portainer verwaltet dann beide Server
  aus derselben Oberfläche (*Environments → Add environment → Agent*, Verbindung
  über das Tailnet), Beszel zeigt beide Systeme. Apps werden weiterhin pro
  Stack einem Server zugeordnet.
