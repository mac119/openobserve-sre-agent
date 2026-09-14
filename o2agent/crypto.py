"""Phase 8: at-rest encryption for stored conversation data.

Opt-in field-level encryption for the SQLite memory DB. When ``O2_MEMORY_KEY``
is set, sensitive JSON columns (message content, tool-result summaries, resolved
context) are encrypted with Fernet (AES-128-CBC + HMAC-SHA256) using a key
derived from the passphrase via PBKDF2-HMAC-SHA256.

Design choices:
- **Opt-in, backward compatible.** No key -> ``build_cipher`` returns None and
  storage stays plaintext (existing DBs keep working). With a key, new writes are
  encrypted; reads transparently decrypt ``enc:v1:``-prefixed values and pass
  through any legacy plaintext, so a DB can be upgraded in place.
- **Fail-closed on a real error.** If a value is marked encrypted but cannot be
  decrypted (wrong key / corruption), we raise rather than silently returning
  ciphertext or a fabricated value.
- **Don't roll our own crypto.** Uses ``cryptography``'s Fernet. The library is
  an optional dependency; it is only required when a key is configured.
"""
from __future__ import annotations

import base64

_PREFIX = "enc:v1:"
# Application-level salt for key derivation. A fixed salt is acceptable here: the
# threat model is at-rest theft of a local, single-tenant DB file, and the salt's
# job is to bind the KDF to this app + defeat generic rainbow tables, not to add
# per-record entropy (Fernet already adds a random IV per token).
_SALT = b"o2agent.memory.v1"
_ITERATIONS = 200_000


class CryptoUnavailable(RuntimeError):
    """Raised when a memory key is configured but ``cryptography`` is missing."""


class MemoryCipher:
    """Transparent field cipher. Encrypts to ``enc:v1:<token>``; decrypts that
    form and passes through legacy plaintext unchanged."""

    def __init__(self, passphrase: str):
        try:
            from cryptography.fernet import Fernet
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        except Exception as e:  # optional dependency not installed
            raise CryptoUnavailable(
                "O2_MEMORY_KEY is set but the 'cryptography' package is not "
                "installed. Run `pip install cryptography` or unset the key."
            ) from e
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(), length=32, salt=_SALT,
            iterations=_ITERATIONS,
        )
        key = base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))
        self._f = Fernet(key)

    def encrypt(self, plaintext: str) -> str:
        if plaintext is None:
            return plaintext
        token = self._f.encrypt(plaintext.encode("utf-8")).decode("ascii")
        return _PREFIX + token

    def decrypt(self, value: str) -> str:
        # Pass through non-strings, empties, and legacy plaintext unchanged.
        if not isinstance(value, str) or not value.startswith(_PREFIX):
            return value
        from cryptography.fernet import InvalidToken
        try:
            raw = self._f.decrypt(value[len(_PREFIX):].encode("ascii"))
        except InvalidToken as e:  # wrong key or corrupted data -> fail closed
            raise ValueError(
                "failed to decrypt stored memory value (wrong O2_MEMORY_KEY or "
                "corrupted data)"
            ) from e
        return raw.decode("utf-8")


def build_cipher(memory_key: str | None) -> MemoryCipher | None:
    """Return a cipher when a passphrase is configured, else None (plaintext)."""
    if not memory_key:
        return None
    return MemoryCipher(memory_key)
