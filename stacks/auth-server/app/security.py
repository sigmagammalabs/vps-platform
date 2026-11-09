"""Sicherheitsbausteine des Lizenzservers.

Konfiguration mit Fail-Fast, zeitkonstanter Vergleich, Anfragebremse und
Admin-Sitzungen. Alles im Speicher: Der Server laeuft als ein Prozess; nach
einem Neustart muss sich der Admin neu anmelden, die Zaehler beginnen bei 0.
"""

from __future__ import annotations

import hmac
import math
import os
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Deque, Dict, Mapping, Optional, Tuple

MIN_ADMIN_TOKEN_LENGTH = 24

# Werte, die jemals im oeffentlichen Repository standen oder als Platzhalter
# dienten. Der Server startet mit keinem davon.
KNOWN_WEAK_TOKENS = frozenset(
    {
        "sglabs-admin-secure-token-2026",
        "hier-ein-starkes-geheimes-admin-passwort-eintragen",
    }
)


class ConfigError(RuntimeError):
    """Fehlende oder unsichere Konfiguration - der Server startet dann nicht."""


@dataclass(frozen=True)
class Settings:
    admin_token: str
    data_dir: Path
    cookie_secure: bool


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    token = env.get("AUTH_ADMIN_TOKEN", "").strip()
    if not token:
        raise ConfigError(
            "AUTH_ADMIN_TOKEN ist nicht gesetzt. Erzeugen mit: "
            "openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | cut -c1-40"
        )
    if token in KNOWN_WEAK_TOKENS:
        raise ConfigError("AUTH_ADMIN_TOKEN ist ein bekannter Standardwert aus dem Repository.")
    if len(token) < MIN_ADMIN_TOKEN_LENGTH:
        raise ConfigError(f"AUTH_ADMIN_TOKEN ist zu kurz (mindestens {MIN_ADMIN_TOKEN_LENGTH} Zeichen).")

    return Settings(
        admin_token=token,
        data_dir=Path(env.get("AUTH_DATA_DIR", "/app/data")),
        cookie_secure=env.get("AUTH_COOKIE_SECURE", "1").strip().lower() not in ("0", "false", "no"),
    )


def constant_time_equals(a: str, b: str) -> bool:
    """Vergleich ohne Zeitunterschied (verhindert Raten Zeichen fuer Zeichen)."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def client_ip(headers: Mapping[str, str], socket_host: Optional[str]) -> str:
    """Adresse des Aufrufers.

    Tailscale Serve/Funnel setzt X-Forwarded-For selbst und ueberschreibt dabei
    Werte des Aufrufers (getestet: ein mitgesendetes 6.6.6.6 kam nicht an).
    Beim direkten Zugriff gilt die Socket-Adresse. Es wird bewusst der LETZTE
    Eintrag genommen, nicht der erste.
    """
    forwarded = headers.get("x-forwarded-for", "")
    last = forwarded.split(",")[-1].strip() if forwarded else ""
    if last:
        return last[:64]
    return socket_host or "unknown"


class SlidingWindowLimiter:
    """Zaehlt Ereignisse je Schluessel in einem gleitenden Zeitfenster."""

    MAX_KEYS = 20_000

    def __init__(self, limit: int, window_seconds: float, clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.window = window_seconds
        self._clock = clock
        self._hits: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def _live(self, key: str, now: float) -> Optional[Deque[float]]:
        queue = self._hits.get(key)
        if queue is None:
            return None
        cutoff = now - self.window
        while queue and queue[0] <= cutoff:
            queue.popleft()
        if not queue:
            del self._hits[key]
            return None
        return queue

    def retry_after(self, key: str) -> int:
        """0 = erlaubt, sonst Sekunden, bis wieder Platz im Fenster ist."""
        with self._lock:
            now = self._clock()
            queue = self._live(key, now)
            if queue is None or len(queue) < self.limit:
                return 0
            return max(1, math.ceil(queue[0] + self.window - now))

    def hit(self, key: str) -> None:
        with self._lock:
            now = self._clock()
            if len(self._hits) >= self.MAX_KEYS:
                for stale in list(self._hits):
                    self._live(stale, now)
                if len(self._hits) >= self.MAX_KEYS:  # nur wenn wirklich alle aktiv sind
                    self._hits.clear()
            self._hits.setdefault(key, deque()).append(now)

    def check_and_hit(self, key: str) -> int:
        """Registriert den Zugriff; liefert die Wartezeit (0 = erlaubt)."""
        wait = self.retry_after(key)
        if wait == 0:
            self.hit(key)
        return wait


class AdminSessions:
    """Anmeldungen am Admin-Dashboard (Cookie) samt CSRF-Token je Sitzung."""

    MAX_SESSIONS = 50

    def __init__(self, ttl_seconds: float = 8 * 3600, clock: Callable[[], float] = time.monotonic):
        self.ttl = ttl_seconds
        self._clock = clock
        self._sessions: Dict[str, Tuple[float, str]] = {}
        self._lock = threading.Lock()

    def create(self) -> str:
        with self._lock:
            now = self._clock()
            self._sessions = {sid: v for sid, v in self._sessions.items() if v[0] > now}
            while len(self._sessions) >= self.MAX_SESSIONS:
                oldest = min(self._sessions, key=lambda sid: self._sessions[sid][0])
                del self._sessions[oldest]
            sid = secrets.token_urlsafe(32)
            self._sessions[sid] = (now + self.ttl, secrets.token_urlsafe(24))
            return sid

    def csrf_for(self, sid: Optional[str]) -> Optional[str]:
        """CSRF-Token der Sitzung, oder None bei unbekannter/abgelaufener Sitzung."""
        if not sid:
            return None
        with self._lock:
            entry = self._sessions.get(sid)
            if entry is None:
                return None
            if entry[0] <= self._clock():
                del self._sessions[sid]
                return None
            return entry[1]

    def delete(self, sid: Optional[str]) -> None:
        if sid:
            with self._lock:
                self._sessions.pop(sid, None)


@dataclass
class Limiters:
    """Alle Anfragebremsen des Servers (je Aufrufer-Adresse)."""

    verify_all: SlidingWindowLimiter  # alle Verifikationen
    verify_fail: SlidingWindowLimiter  # nur unbekannte Schluessel (Raten)
    admin_fail: SlidingWindowLimiter  # falsche Admin-Anmeldungen

    @classmethod
    def default(cls) -> "Limiters":
        return cls(
            verify_all=SlidingWindowLimiter(limit=30, window_seconds=60),
            verify_fail=SlidingWindowLimiter(limit=10, window_seconds=600),
            admin_fail=SlidingWindowLimiter(limit=5, window_seconds=300),
        )
