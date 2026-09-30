#!/usr/bin/env bash
#
# backup.sh -- sichert alles, was sich nicht aus Git wiederherstellen laesst:
#
#   portainer_data   Stacks inkl. ihrer Umgebungsvariablen (= API-Keys!)
#   beszel_data      Monitoring-Verlauf, Alarm-Einstellungen
#   /srv/apps        Daten und Logs aller Apps (z. B. purchases.csv)
#   platform/.env    Plattform-Einstellungen
#
# Portainer und Beszel werden dafuer kurz angehalten (konsistente Datenbanken).
# Die Archive liegen auf derselben Platte - fuer echte Ausfallsicherheit
# zusaetzlich Hetzner-Backups aktivieren oder das Zielverzeichnis extern
# spiegeln (docs/betrieb.md).
#
#   sudo bash scripts/backup.sh [ZIELVERZEICHNIS]     Standard: /var/backups/platform
#   KEEP_DAYS=30 sudo bash scripts/backup.sh          Aufbewahrung (Standard: 14 Tage)
#
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-/var/backups/platform}"
KEEP_DAYS="${KEEP_DAYS:-14}"
STAMP="$(date +%Y-%m-%d_%H%M)"

log() { printf '==> %s\n' "$*"; }
die() { printf 'FEHLER: %s\n' "$*" >&2; exit 1; }

[[ ${EUID:-$(id -u)} -eq 0 ]] || die "Bitte mit sudo ausfuehren."
install -d -m 700 "$TARGET"

# backup_volume <volume> <container, der es schreibt>
backup_volume() {
    local volume=$1 container=$2 was_running=0 rc=0
    if ! docker volume inspect "$volume" >/dev/null 2>&1; then
        printf '  -- %s existiert nicht, uebersprungen\n' "$volume"
        return
    fi
    if [[ -n "$(docker ps -q -f "name=^${container}$")" ]]; then
        docker stop "$container" >/dev/null
        was_running=1
    fi
    docker run --rm -v "$volume":/data:ro -v "$TARGET":/backup busybox:stable \
        tar czf "/backup/${volume}_${STAMP}.tar.gz" -C /data . || rc=$?
    # Auch nach einem Fehler wieder starten, sonst bleibt der Dienst aus.
    (( was_running )) && docker start "$container" >/dev/null
    (( rc == 0 )) || die "Sicherung von $volume fehlgeschlagen (Exit $rc)"
    printf '  ok %s\n' "${volume}_${STAMP}.tar.gz"
}

log "Sicherung nach $TARGET"
backup_volume portainer_data portainer
backup_volume beszel_data beszel

if [[ -d /srv/apps ]]; then
    tar czf "$TARGET/srv-apps_${STAMP}.tar.gz" -C /srv apps
    printf '  ok %s\n' "srv-apps_${STAMP}.tar.gz"
fi

# .env-Dateien: Plattform und ggf. per CLI betriebene Stacks
mapfile -t env_files < <(cd "$ROOT" && find platform stacks -maxdepth 2 -name .env -type f 2>/dev/null)
if (( ${#env_files[@]} )); then
    tar czf "$TARGET/env-files_${STAMP}.tar.gz" -C "$ROOT" "${env_files[@]}"
    chmod 600 "$TARGET/env-files_${STAMP}.tar.gz"
    printf '  ok %s\n' "env-files_${STAMP}.tar.gz"
fi

log "Archive aelter als $KEEP_DAYS Tage entfernen"
find "$TARGET" -maxdepth 1 -name '*.tar.gz' -type f -mtime +"$KEEP_DAYS" -print -delete

log "fertig"
du -sh "$TARGET"
