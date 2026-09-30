#!/usr/bin/env bash
#
# tailscale-serve.sh -- gibt die lokalen Weboberflaechen aus platform/serve.conf
# per HTTPS im eigenen Tailnet frei (Tailscale Serve - NICHT oeffentlich, das
# waere Tailscale Funnel und wird hier nie verwendet).
#
#   bash scripts/tailscale-serve.sh apply    Freigaben neu setzen (Standard)
#   bash scripts/tailscale-serve.sh status
#   bash scripts/tailscale-serve.sh reset    alle Freigaben entfernen
#
# Die Freigaben ueberstehen Neustarts (tailscaled speichert sie selbst).
# Voraussetzung: MagicDNS und "HTTPS Certificates" in der Tailscale-Adminkonsole.
#
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF="$ROOT/platform/serve.conf"

die() { printf 'FEHLER: %s\n' "$*" >&2; exit 1; }

command -v tailscale >/dev/null 2>&1 || die "tailscale fehlt - zuerst bootstrap/host-setup.sh ausfuehren."
SUDO=()
[[ ${EUID:-$(id -u)} -eq 0 ]] || SUDO=(sudo)

apply() {
    [[ -f "$CONF" ]] || die "$CONF fehlt"
    # Erst alles entfernen, dann neu setzen: so verschwinden auch Eintraege,
    # die aus serve.conf geloescht wurden.
    "${SUDO[@]}" tailscale serve reset
    local port target name
    while read -r port target name <&3; do
        [[ -z "$port" || "$port" == \#* ]] && continue
        printf '  %-10s :%-5s -> %s\n' "${name:-?}" "$port" "$target"
        # Ist HTTPS im Tailnet noch nicht aktiviert, gibt tailscale hier einen
        # Link zum Freischalten aus und wartet - Ausgabe daher nicht unterdruecken.
        "${SUDO[@]}" tailscale serve --bg --https="$port" "$target"
    done 3< "$CONF"
    printf '\n'
    "${SUDO[@]}" tailscale serve status
}

case "${1:-apply}" in
    apply)  apply ;;
    status) "${SUDO[@]}" tailscale serve status ;;
    reset)  "${SUDO[@]}" tailscale serve reset ;;
    *)      awk 'NR<3 {next} /^#/ {sub(/^# ?/, ""); print; next} {exit}' "$0"; exit 1 ;;
esac
