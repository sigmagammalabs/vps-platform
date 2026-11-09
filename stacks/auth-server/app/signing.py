"""Ed25519-Signierung der Lizenz-Token (Server-Vertrag v2).

Token = <payload_b64url>.<signature_b64url>. Signiert werden die ASCII-Bytes des
Strings payload_b64url (nicht das dekodierte JSON). Der Client kennt nur den
oeffentlichen Schluessel; der private bleibt auf dem Server.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Dict

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


class SigningKeyError(RuntimeError):
    """Der Signaturschluessel ist unbrauchbar - der Server startet dann nicht."""


class Signer:
    def __init__(self, private_key: Ed25519PrivateKey):
        self._key = private_key
        raw = private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.public_key_b64 = base64.b64encode(raw).decode("ascii")

    @classmethod
    def load_or_create(cls, path: Path) -> "Signer":
        """Laedt den Schluessel oder erzeugt ihn beim allerersten Start.

        Eine vorhandene, aber unlesbare Datei fuehrt bewusst zum Abbruch: Ein
        stillschweigend neu erzeugter Schluessel wuerde alle ausgelieferten
        Clients entwerten.
        """
        if path.exists():
            try:
                key = serialization.load_pem_private_key(path.read_bytes(), password=None)
            except Exception as exc:
                raise SigningKeyError(
                    f"Signaturschluessel {path} ist nicht lesbar ({exc}). Der Server startet "
                    "absichtlich nicht: ein neuer Schluessel wuerde alle Clients entwerten. "
                    "Datei aus der Sicherung wiederherstellen."
                ) from exc
            if not isinstance(key, Ed25519PrivateKey):
                raise SigningKeyError(f"{path} enthaelt keinen Ed25519-Schluessel.")
            return cls(key)

        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(pem)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return cls(key)

    def sign_token(self, payload: Dict[str, Any]) -> str:
        payload_b64 = b64url_encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signature = self._key.sign(payload_b64.encode("ascii"))
        return f"{payload_b64}.{b64url_encode(signature)}"
