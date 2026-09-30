#!/usr/bin/env bash
#
# host-setup.sh -- macht aus einem frischen Hetzner-VPS (Ubuntu 24.04 LTS oder
# Debian 12/13) einen Docker-Host, der nur ueber Tailscale administriert wird:
#
#   1. Systemupdate, Basispakete, automatische Sicherheitsupdates
#   2. Zeitzone, Swap (nur bei < 8 GB RAM und noch ohne Swap)
#   3. Docker Engine + Compose-Plugin aus dem offiziellen Docker-Repository,
#      Log-Rotation fuer alle Container
#   4. Tailscale inkl. Anmeldung im Tailnet
#   5. Firewall (UFW): eingehend nur SSH und Tailscale
#   6. SSH: nur Schluessel, keine Passwoerter
#   7. Verzeichnis /srv/apps fuer App-Daten
#   8. Wartung: Log-Rotation (App-Logs, Systemjournal), naechtliche Sicherung
#
# Idempotent - erneutes Ausfuehren ist unschaedlich.
#
# Aufruf als root, aus dem geklonten Repo unter /opt/platform:
#
#   bash bootstrap/host-setup.sh
#   bash bootstrap/host-setup.sh --admin-user sebas
#   bash bootstrap/host-setup.sh --ssh-tailscale-only   # spaeter, siehe README
#
set -Eeuo pipefail

TS_HOSTNAME="vps"
TS_AUTHKEY="${TS_AUTHKEY:-}"
TIMEZONE="Europe/Berlin"
ADMIN_USER=""
SSH_TAILSCALE_ONLY=0
TAILSCALE_SSH=0
SWAP_SIZE="2G"
APPS_DIR="/srv/apps"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-/opt/platform/bootstrap/x}")/.." && pwd)"
LOG_FILE="/var/log/host-setup.log"

# ---------------------------------------------------------------------------
# Ausgabe
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
    C_RESET=$'\033[0m'; C_INFO=$'\033[1;34m'; C_OK=$'\033[1;32m'
    C_WARN=$'\033[1;33m'; C_ERR=$'\033[1;31m'; C_DIM=$'\033[2m'
else
    C_RESET=''; C_INFO=''; C_OK=''; C_WARN=''; C_ERR=''; C_DIM=''
fi
WARNINGS=()
log()  { printf '%s==>%s %s\n' "$C_INFO" "$C_RESET" "$*"; }
ok()   { printf '%s  ok%s %s\n' "$C_OK" "$C_RESET" "$*"; }
skip() { printf '%s  --%s %s\n' "$C_DIM" "$C_RESET" "$*"; }
warn() { printf '%s  !!%s %s\n' "$C_WARN" "$C_RESET" "$*" >&2; WARNINGS+=("$*"); }
die()  { printf '%sFEHLER:%s %s\n' "$C_ERR" "$C_RESET" "$*" >&2; exit 1; }
trap 'printf "%sAbbruch in Zeile %s.%s Nach dem Beheben einfach erneut ausfuehren.\n" "$C_ERR" "$LINENO" "$C_RESET" >&2' ERR

# Fuehrt ein gespraechiges Kommando (apt, Installer) aus. Die Ausgabe landet
# in $LOG_FILE; nur bei einem Fehler werden die letzten Zeilen angezeigt.
quiet() {
    if ! "$@" >>"$LOG_FILE" 2>&1; then
        printf '%s--- letzte Zeilen aus %s ---%s\n' "$C_ERR" "$LOG_FILE" "$C_RESET" >&2
        tail -n 25 "$LOG_FILE" >&2
        die "fehlgeschlagen: $*"
    fi
}

usage() {
    awk 'NR<3 {next} /^#/ {sub(/^# ?/, ""); print; next} {exit}' "$0"
    cat <<'USAGE'

Optionen:
  --ts-hostname NAME     Name des VPS im Tailnet        (Standard: vps)
  --authkey KEY          Tailscale-Auth-Key statt Login-Link (oder TS_AUTHKEY=...)
  --admin-user NAME      sudo-Benutzer anlegen, root-SSH-Schluessel uebernehmen,
                         danach root-Login per SSH abschalten
  --tailscale-ssh        zusaetzlich Tailscale SSH aktivieren (Zugriff dann ueber
                         die Tailnet-ACL statt ueber authorized_keys)
  --ssh-tailscale-only   SSH aus dem Internet sperren, nur noch ueber das Tailnet
  --timezone TZ          Zeitzone                        (Standard: Europe/Berlin)
  --swap-size GROESSE    Swapfile bei < 8 GB RAM, 0 = keins (Standard: 2G)
  -h, --help             diese Hilfe
USAGE
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ts-hostname)        TS_HOSTNAME=${2:?Name fehlt}; shift 2 ;;
        --authkey)            TS_AUTHKEY=${2:?Key fehlt}; shift 2 ;;
        --admin-user)         ADMIN_USER=${2:?Name fehlt}; shift 2 ;;
        --tailscale-ssh)      TAILSCALE_SSH=1; shift ;;
        --ssh-tailscale-only) SSH_TAILSCALE_ONLY=1; shift ;;
        --timezone)           TIMEZONE=${2:?Zeitzone fehlt}; shift 2 ;;
        --swap-size)          SWAP_SIZE=${2:?Groesse fehlt}; shift 2 ;;
        -h|--help)            usage; exit 0 ;;
        *)                    die "Unbekannte Option: $1 (--help)" ;;
    esac
done

[[ ${EUID:-$(id -u)} -eq 0 ]] || die "Bitte als root ausfuehren (sudo -i)."
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}" in
    ubuntu|debian) ;;
    *) die "Nur Ubuntu/Debian werden unterstuetzt (gefunden: ${ID:-unbekannt})." ;;
esac

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a   # Ubuntu: Dienste nach Updates ohne Rueckfrage neu starten

TS_IP=""
TS_FQDN=""

# ---------------------------------------------------------------------------
# 1. Pakete und automatische Sicherheitsupdates
# ---------------------------------------------------------------------------
setup_packages() {
    log "Systemupdate und Basispakete (Details: $LOG_FILE)"
    quiet apt-get update
    quiet apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold upgrade
    quiet apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git jq sudo ufw unattended-upgrades
    cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
    ok "Pakete aktuell, Sicherheitsupdates laufen automatisch"
    if [[ -f /var/run/reboot-required ]]; then
        warn "Kernel-/Systemupdate eingespielt - nach dem Setup einmal 'reboot'."
    fi
}

# ---------------------------------------------------------------------------
# 2. Zeitzone und Swap
# ---------------------------------------------------------------------------
setup_time_and_swap() {
    log "Zeitzone und Swap"
    timedatectl set-timezone "$TIMEZONE"
    ok "Zeitzone $TIMEZONE"

    if [[ "$SWAP_SIZE" == "0" ]]; then
        skip "Swap abgewaehlt"
        return
    fi
    if [[ -n "$(swapon --show --noheadings)" ]]; then
        ok "Swap bereits vorhanden"
        return
    fi
    local mem_mb
    mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
    if (( mem_mb >= 8192 )); then
        skip "${mem_mb} MB RAM - kein Swap noetig"
        return
    fi
    # Puffer gegen den OOM-Killer, wenn z. B. pandas kurz viel Speicher braucht.
    fallocate -l "$SWAP_SIZE" /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    echo 'vm.swappiness=10' > /etc/sysctl.d/99-swappiness.conf
    sysctl -q -p /etc/sysctl.d/99-swappiness.conf
    ok "Swapfile $SWAP_SIZE angelegt (${mem_mb} MB RAM)"
}

# ---------------------------------------------------------------------------
# 3. Docker
# ---------------------------------------------------------------------------
setup_docker() {
    log "Docker"
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        ok "bereits installiert ($(docker --version))"
    else
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
        chmod a+r /etc/apt/keyrings/docker.asc
        local codename="${UBUNTU_CODENAME:-$VERSION_CODENAME}"
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${codename} stable" \
            > /etc/apt/sources.list.d/docker.list
        quiet apt-get update
        quiet apt-get install -y docker-ce docker-ce-cli containerd.io \
            docker-buildx-plugin docker-compose-plugin
        ok "installiert ($(docker --version))"
    fi

    # Log-Rotation fuer alle Container (sonst wachsen die Logs von Dauer-
    # prozessen unbegrenzt), live-restore haelt Container bei Docker-Updates am
    # Laufen. Gilt fuer neu erstellte Container.
    local desired
    desired=$(cat <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  },
  "live-restore": true
}
EOF
)
    mkdir -p /etc/docker
    if [[ "$(cat /etc/docker/daemon.json 2>/dev/null)" != "$desired" ]]; then
        printf '%s\n' "$desired" > /etc/docker/daemon.json
        systemctl restart docker
        ok "daemon.json geschrieben (Log-Rotation 3 x 10 MB, live-restore)"
    else
        ok "daemon.json aktuell"
    fi
    systemctl enable --now docker >/dev/null 2>&1
}

# ---------------------------------------------------------------------------
# 4. Tailscale
# ---------------------------------------------------------------------------
setup_tailscale() {
    log "Tailscale"
    if ! command -v tailscale >/dev/null 2>&1; then
        quiet sh -c 'curl -fsSL https://tailscale.com/install.sh | sh'
    fi
    systemctl enable --now tailscaled >/dev/null 2>&1

    if tailscale status >/dev/null 2>&1; then
        ok "bereits im Tailnet angemeldet"
        tailscale set --hostname="$TS_HOSTNAME"
        (( TAILSCALE_SSH )) && tailscale set --ssh
    else
        local args=(--hostname="$TS_HOSTNAME")
        (( TAILSCALE_SSH )) && args+=(--ssh)
        [[ -n "$TS_AUTHKEY" ]] && args+=(--auth-key="$TS_AUTHKEY")
        [[ -z "$TS_AUTHKEY" ]] && log "Gleich erscheint ein Login-Link - im Browser oeffnen und den VPS freigeben."
        tailscale up "${args[@]}"
    fi

    TS_IP=$(tailscale ip -4 2>/dev/null | head -n1 || true)
    TS_FQDN=$(tailscale status --json 2>/dev/null | jq -r '.Self.DNSName // empty' | sed 's/\.$//')
    [[ -n "$TS_IP" ]] || die "Tailscale hat keine IP - 'tailscale status' pruefen."
    ok "verbunden als ${TS_FQDN:-?} ($TS_IP)"
}

# ---------------------------------------------------------------------------
# 5. Firewall
# ---------------------------------------------------------------------------
# Hinweis: Docker schreibt eigene iptables-Regeln und umgeht UFW bei
# veroeffentlichten Ports. Deshalb binden alle Stacks ihre Ports nur an
# 127.0.0.1 - UFW regelt hier nur, was den Host selbst erreicht.
setup_firewall() {
    log "Firewall (UFW)"
    ufw default deny incoming >/dev/null
    ufw default allow outgoing >/dev/null
    ufw allow in on tailscale0 comment 'Tailnet' >/dev/null
    ufw allow 41641/udp comment 'Tailscale Direktverbindung' >/dev/null

    if (( SSH_TAILSCALE_ONLY )); then
        ufw delete allow 22/tcp >/dev/null 2>&1 || true
        ufw delete allow OpenSSH >/dev/null 2>&1 || true
        ok "SSH aus dem Internet gesperrt - nur noch: ssh ${ADMIN_USER:-root}@${TS_FQDN:-$TS_IP}"
    else
        ufw allow 22/tcp comment 'SSH' >/dev/null
        ok "SSH (22/tcp) offen - nach dem Test ueber Tailscale mit --ssh-tailscale-only schliessen"
    fi
    ufw --force enable >/dev/null
    ok "aktiv: eingehend nur Tailnet$( (( SSH_TAILSCALE_ONLY )) || printf ' + SSH')"
}

# ---------------------------------------------------------------------------
# 6. SSH
# ---------------------------------------------------------------------------
setup_ssh() {
    log "SSH"
    if [[ -n "$ADMIN_USER" ]]; then
        if ! id -u "$ADMIN_USER" >/dev/null 2>&1; then
            useradd --create-home --shell /bin/bash "$ADMIN_USER"
            # "*" = kein Passwort, aber nicht gesperrt ("!" wuerde je nach
            # sshd-Konfiguration auch den Schluessel-Login verhindern).
            usermod -p '*' "$ADMIN_USER"
            ok "Benutzer $ADMIN_USER angelegt"
        fi
        usermod -aG sudo,docker "$ADMIN_USER"
        # Kein Passwort gesetzt -> sudo ohne Passwort, Zugang nur per Schluessel.
        echo "$ADMIN_USER ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/90-$ADMIN_USER"
        chmod 440 "/etc/sudoers.d/90-$ADMIN_USER"
        visudo -cqf "/etc/sudoers.d/90-$ADMIN_USER" || die "sudoers-Datei ungueltig"

        local home; home=$(getent passwd "$ADMIN_USER" | cut -d: -f6)
        if [[ ! -s "$home/.ssh/authorized_keys" && -s /root/.ssh/authorized_keys ]]; then
            install -d -m 700 -o "$ADMIN_USER" -g "$ADMIN_USER" "$home/.ssh"
            install -m 600 -o "$ADMIN_USER" -g "$ADMIN_USER" /root/.ssh/authorized_keys "$home/.ssh/authorized_keys"
            ok "SSH-Schluessel von root uebernommen"
        fi
    fi

    if [[ ! -s /root/.ssh/authorized_keys ]] && \
       [[ -z "$ADMIN_USER" || ! -s "$(getent passwd "$ADMIN_USER" | cut -d: -f6)/.ssh/authorized_keys" ]]; then
        warn "Kein SSH-Schluessel hinterlegt - Passwort-Login bleibt vorerst aktiv."
        return
    fi

    local conf=/etc/ssh/sshd_config.d/10-hardening.conf
    local root_login="prohibit-password"
    if [[ -n "$ADMIN_USER" && -s "$(getent passwd "$ADMIN_USER" | cut -d: -f6)/.ssh/authorized_keys" ]]; then
        root_login="no"
    elif grep -qx 'PermitRootLogin no' "$conf" 2>/dev/null; then
        # Frueherer Lauf mit --admin-user: beim erneuten Aufruf ohne die
        # Option root-Login nicht still wieder einschalten.
        root_login="no"
    fi

    # "10-" gewinnt gegen z. B. 50-cloud-init.conf: sshd nimmt den ersten Wert.
    cat > "$conf" <<EOF
# von bootstrap/host-setup.sh
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin $root_login
X11Forwarding no
EOF
    mkdir -p /run/sshd
    if ! sshd -t; then
        rm -f "$conf"
        die "sshd-Konfiguration ungueltig - Aenderung zurueckgenommen."
    fi
    systemctl try-reload-or-restart ssh 2>/dev/null || systemctl try-reload-or-restart sshd 2>/dev/null || true
    ok "nur Schluessel, PermitRootLogin=$root_login"
    if [[ "$root_login" == "no" ]]; then
        warn "root-Login per SSH ist ab jetzt aus. VOR dem Schliessen dieser Sitzung in einem zweiten Terminal testen: ssh $ADMIN_USER@<server>"
    fi
}

# ---------------------------------------------------------------------------
# 7. Verzeichnisse
# ---------------------------------------------------------------------------
setup_dirs() {
    log "Verzeichnisse"
    install -d -m 755 "$APPS_DIR"
    ok "$APPS_DIR (Daten der App-Stacks)"

    # Die App-Images laufen als UID 10001. Ein gleichnamiger Systembenutzer auf
    # dem Host macht Besitzer lesbar (ls zeigt "app") und wird fuer logrotate
    # gebraucht, das bei "su" einen Namen statt einer Nummer verlangt.
    if ! getent passwd 10001 >/dev/null; then
        getent group 10001 >/dev/null || groupadd --system --gid 10001 app
        useradd --system --uid 10001 --gid 10001 --no-create-home \
            --home-dir /nonexistent --shell /usr/sbin/nologin app
    fi
    ok "Benutzer $(getent passwd 10001 | cut -d: -f1) (UID 10001) = Benutzer in den App-Containern"
}

# ---------------------------------------------------------------------------
# 8. Wartung: Log-Rotation, Journal-Groesse, naechtliche Sicherung
# ---------------------------------------------------------------------------
BACKUP_TIME="${BACKUP_TIME:-30 3 * * *}"   # Cron-Zeit (Serverzeit), Standard 03:30

setup_maintenance() {
    log "Wartung (Logs, Sicherung)"

    # Datei-Logs, die weder Docker noch die App selbst rotiert.
    local rot_src="$REPO_DIR/platform/logrotate.conf"
    if [[ -f "$rot_src" ]]; then
        install -m 644 "$rot_src" /etc/logrotate.d/platform
        # -d = Trockenlauf: prueft die Syntax, veraendert nichts
        if logrotate -d /etc/logrotate.d/platform 2>&1 | grep -q '^error:'; then
            warn "logrotate meldet Fehler in /etc/logrotate.d/platform - 'logrotate -d /etc/logrotate.d/platform' pruefen."
        else
            ok "Log-Rotation der App-Logs (/etc/logrotate.d/platform)"
        fi
    else
        warn "$rot_src fehlt - Log-Rotation der App-Logs nicht eingerichtet."
    fi

    # Systemjournal begrenzen - der Standard erlaubt 10 % der Platte (bis 4 GB).
    local jconf=/etc/systemd/journald.conf.d/10-platform.conf
    local jdesired=$'[Journal]\nSystemMaxUse=500M'
    if [[ "$(cat "$jconf" 2>/dev/null)" != "$jdesired" ]]; then
        install -d -m 755 /etc/systemd/journald.conf.d
        printf '%s\n' "$jdesired" > "$jconf"
        systemctl restart systemd-journald
    fi
    ok "Systemjournal auf 500 MB begrenzt"

    # Naechtliche Sicherung: Portainer (inkl. Stack-Secrets), Beszel, /srv/apps.
    cat > /etc/cron.d/platform-backup <<EOF
# von bootstrap/host-setup.sh - naechtliche Sicherung, siehe scripts/backup.sh
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
MAILTO=""
$BACKUP_TIME root /bin/bash $REPO_DIR/scripts/backup.sh >> /var/log/platform-backup.log 2>&1
EOF
    chmod 644 /etc/cron.d/platform-backup
    ok "Sicherung per Cron ($BACKUP_TIME) nach /var/backups/platform, Log: /var/log/platform-backup.log"
}

summary() {
    local base="https://${TS_FQDN:-<vps>.<tailnet>.ts.net}"
    printf '\n%s============================================================%s\n' "$C_INFO" "$C_RESET"
    printf '  Host-Setup abgeschlossen\n'
    printf '%s============================================================%s\n\n' "$C_INFO" "$C_RESET"
    if (( ${#WARNINGS[@]} )); then
        printf '  %sOffene Punkte:%s\n' "$C_WARN" "$C_RESET"
        local w; for w in "${WARNINGS[@]}"; do printf '    - %s\n' "$w"; done
        printf '\n'
    fi
    cat <<EOF
  Tailnet:  ${TS_FQDN:-?}  ($TS_IP)

  Naechste Schritte:
    1. Tailscale-Adminkonsole -> DNS: MagicDNS und "HTTPS Certificates" aktivieren
    2. Plattform starten:   bash $REPO_DIR/scripts/platform.sh up
    3. Im Browser (Geraet im Tailnet):
         $base          Startseite
         $base:9443     Portainer - Admin-Konto innerhalb von 5 Minuten anlegen!
EOF
}

main() {
    setup_packages
    setup_time_and_swap
    setup_docker
    setup_tailscale
    setup_firewall
    setup_ssh
    setup_dirs
    setup_maintenance
    summary
}

main "$@"
