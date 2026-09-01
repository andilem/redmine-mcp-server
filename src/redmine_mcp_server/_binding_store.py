"""Persist token bindings so a restart does not sign everyone out.

The stored payload is encrypted under a key derived from the client's own
token. The server therefore cannot decrypt anything at rest: whoever holds
only the volume holds ciphertexts and token hashes, and nothing else. The key
material arrives with each request, in the token the client already keeps.

That is the whole point of deriving from the token rather than from a
server-side secret. A secret in the server's environment sits next to the
ciphertext it protects, so anyone who reaches the host reads every binding at
once. Here, reaching the host reveals only the bindings of users who make a
request while the attacker is watching.

Layout under the store directory::

    tokens/<sha256(token)>.json   {"v", "salt", "nonce", "ct", "exp"}
    clients/<sha256(client_id)>.json   the DCR registration, plaintext

Token files are named by a hash, so the token itself is not recoverable from
a directory listing. ``exp`` stays in the clear so expired files can be
pruned without being able to read them; it reveals when a session lapses and
nothing more.

Client registrations are *not* encrypted. Dynamic client registration is open
on this server, so an attacker can mint an equivalent registration at any
time -- encrypting it would protect nothing while costing the ability to
rehydrate a client before its token arrives.
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)

_INFO = b"redmine-mcp/binding/v1"
_VERSION = 1


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _token_id(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _derive(token: str, salt: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=_INFO).derive(
        token.encode("utf-8")
    )


class BindingStore:
    """Token-encrypted bindings on disk, plus plaintext client registrations."""

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self._root = Path(directory)
        self._tokens = self._root / "tokens"
        self._clients = self._root / "clients"
        for path in (self._root, self._tokens, self._clients):
            path.mkdir(parents=True, exist_ok=True)
            try:
                path.chmod(0o700)
            except OSError:  # pragma: no cover - not all filesystems allow it
                pass

    # --- tokens ------------------------------------------------------

    def put_token(self, token: str, payload: dict[str, Any]) -> None:
        """Encrypt ``payload`` under ``token`` and store it."""
        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(12)
        plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ct = AESGCM(_derive(token, salt)).encrypt(nonce, plaintext, None)
        record = {
            "v": _VERSION,
            "salt": _b64(salt),
            "nonce": _b64(nonce),
            "ct": _b64(ct),
            "exp": payload.get("expires_at"),
        }
        self._write(self._tokens / f"{_token_id(token)}.json", record)

    def get_token(self, token: str) -> Optional[dict[str, Any]]:
        """Return the payload stored under ``token``, or ``None``.

        ``None`` covers every failure alike -- absent, unreadable, expired, or
        a ciphertext this token cannot open. A caller cannot tell them apart,
        and does not need to.
        """
        path = self._tokens / f"{_token_id(token)}.json"
        record = self._read(path)
        if record is None:
            return None
        if record.get("v") != _VERSION:
            logger.info("Binding store: ignoring record with unknown version.")
            return None
        exp = record.get("exp")
        if exp is not None and exp < time.time():
            self._unlink(path)
            return None
        try:
            key = _derive(token, _unb64(record["salt"]))
            raw = AESGCM(key).decrypt(
                _unb64(record["nonce"]), _unb64(record["ct"]), None
            )
        except (InvalidTag, KeyError, ValueError):
            # Wrong token for this file, or a corrupt record.
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:  # pragma: no cover - would mean we wrote garbage
            return None

    def delete_token(self, token: str) -> None:
        self._unlink(self._tokens / f"{_token_id(token)}.json")

    # --- client registrations ----------------------------------------

    def put_client(self, client_id: str, data: dict[str, Any]) -> None:
        self._write(self._clients / f"{_token_id(client_id)}.json", data)

    def get_client(self, client_id: str) -> Optional[dict[str, Any]]:
        return self._read(self._clients / f"{_token_id(client_id)}.json")

    # --- housekeeping ------------------------------------------------

    def prune(self) -> int:
        """Delete token records whose expiry has passed. Returns the count."""
        now = time.time()
        removed = 0
        for path in self._tokens.glob("*.json"):
            record = self._read(path)
            if record is None:
                continue
            exp = record.get("exp")
            if exp is not None and exp < now:
                self._unlink(path)
                removed += 1
        return removed

    # --- io ----------------------------------------------------------

    def _write(self, path: Path, record: dict[str, Any]) -> None:
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(record, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, path)
            try:
                path.chmod(0o600)
            except OSError:  # pragma: no cover
                pass
        except OSError as exc:
            logger.warning("Binding store: could not write %s: %s", path.name, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:  # pragma: no cover
                pass

    def _read(self, path: Path) -> Optional[dict[str, Any]]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            logger.warning("Binding store: could not read %s: %s", path.name, exc)
            return None

    def _unlink(self, path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover
            logger.warning("Binding store: could not delete %s: %s", path.name, exc)
