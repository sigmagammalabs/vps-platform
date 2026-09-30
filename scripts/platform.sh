#!/usr/bin/env bash
#
# platform.sh -- Plattform-Stack (Portainer, Homepage, Beszel, Dozzle, Ofelia)
# starten, aktualisieren und pruefen.
#
#   bash scripts/platform.sh up        starten bzw. Konfigurationsaenderungen anwenden
#   bash scripts/platform.sh update    neue Images ziehen und neu starten
#   bash scripts/platform.sh status    Container, Tailscale-Freigaben, URLs
#   bash scripts/platform.sh logs [dienst]
#   bash scripts/platform.sh down      alles stoppen (Daten bleiben erhalten)
#
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIR="$ROOT/platform"
ENV_FILE="$DIR/.env"

log()  { printf '==> %s\n' "$*"; }
warn() { printf '  !! %s\n' "$*" >&2; }
die()  { printf 'FEHLER: %s\n' "$*" >&2; exit 1; }

env_value() { grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2- || true; }

ensure_env() {
    command -v docker >/dev/null 2>&1 || die "Docker fehlt - zuerst bootstrap/host-setup.sh ausfuehren."
    if [[ ! -f "$ENV_FILE" ]]; then
        install -m 600 "$DIR/.env.example" "$ENV_FILE"
        log "platform/.env aus Vorlage angelegt"
    fi
    if [[ -z "$(env_value TS_FQDN)" ]]; then
        local fqdn
        fqdn=$(tailscale status --json 2>/dev/null | jq -r '.Self.DNSName // empty' | sed 's/\.$//' || true)
        [[ -n "$fqdn" ]] || die "TS_FQDN ist leer und Tailscale liefert keinen Namen. 'tailscale status' pruefen oder TS_FQDN in platform/.env eintragen."
        if grep -qE '^TS_FQDN=' "$ENV_FILE"; then
            sed -i "s|^TS_FQDN=.*|TS_FQDN=$fqdn|" "$ENV_FILE"
        else
            echo "TS_FQDN=$fqdn" >> "$ENV_FILE"
        fi
        log "TS_FQDN=$fqdn in platform/.env eingetragen"
    fi
}

agent_configured() {
    [[ -n "$(env_value BESZEL_AGENT_KEY)" && -n "$(env_value BESZEL_AGENT_TOKEN)" ]]
}

compose() {
    local profiles=()
    agent_configured && profiles=(--profile agent)
    docker compose --project-directory "$DIR" --env-file "$ENV_FILE" \
        -f "$DIR/compose.yaml" "${profiles[@]}" "$@"
}

print_urls() {
    local fqdn port target name
    fqdn=$(env_value TS_FQDN)
    printf '\n  Erreichbar von jedem Geraet im Tailnet:\n'
    while read -r port target name <&3; do
        [[ -z "$port" || "$port" == \#* ]] && continue
        if [[ "$port" == "443" ]]; then
            printf '    %-10s https://%s\n' "$name" "$fqdn"
        else
            printf '    %-10s https://%s:%s\n' "$name" "$fqdn" "$port"
        fi
    done 3< "$DIR/serve.conf"
    printf '\n'
}

cmd_up() {
    ensure_env
    log "Plattform starten"
    compose up -d --remove-orphans
    log "Tailscale-Freigaben anwenden"
    bash "$ROOT/scripts/tailscale-serve.sh" apply || warn "Tailscale Serve fehlgeschlagen - siehe Ausgabe oben."
    print_urls
    if ! agent_configured; then
        warn "Beszel-Agent noch nicht eingerichtet (BESZEL_AGENT_KEY/TOKEN in platform/.env) - docs/betrieb.md"
    fi
    printf '  Portainer: Admin-Konto innerhalb von 5 Minuten nach dem ersten Start anlegen,\n'
    printf '  sonst sperrt sich die Einrichtung (dann: docker restart portainer).\n\n'
}

cmd_update() {
    ensure_env
    log "Images aktualisieren"
    compose pull
    compose up -d --remove-orphans
    docker image prune -f >/dev/null
    log "fertig"
    compose ps
}

cmd_status() {
    ensure_env
    compose ps
    printf '\n'
    tailscale serve status 2>/dev/null || warn "tailscale serve status nicht verfuegbar"
    print_urls
}

cmd_logs()  { ensure_env; compose logs -f --tail=100 "$@"; }
cmd_down()  { ensure_env; compose down --remove-orphans; }

case "${1:-}" in
    up)      shift; cmd_up "$@" ;;
    update)  shift; cmd_update "$@" ;;
    status)  shift; cmd_status "$@" ;;
    logs)    shift; cmd_logs "$@" ;;
    down)    shift; cmd_down "$@" ;;
    *)       awk 'NR<3 {next} /^#/ {sub(/^# ?/, ""); print; next} {exit}' "$0"; exit 1 ;;
esac
