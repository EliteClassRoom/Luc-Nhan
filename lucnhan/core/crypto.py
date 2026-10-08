"""Optional API key encryption using PBKDF2 + AES-256-GCM.

The ``cryptography`` package is a transitive dependency of ``anthropic``,
``openai``, and ``mcp`` (via ``httpx``).  If it is not available in the
host Python environment the feature degrades gracefully — ``is_available()``
returns False and the settings checkbox is disabled.

The initial import of this module is intentionally cheap: it does NOT
import the ``cryptography`` package.  ``is_available()`` is a fast check
that uses :mod:`importlib.util.find_spec` so the settings dialog can
paint before any heavy crypto dependency is loaded.  The crypto module
is only imported on the first call to :func:`encrypt_keys` or
:func:`decrypt_keys`.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
from typing import Any

# Persisted format marker written into new encrypted key blocks.
_VERIFY_SENTINEL = "lucnhan-encryption-v1"
#: Old spelling, still accepted on read: blocks encrypted before the rename
#: carry it *inside* their ciphertext, so refusing it would strand the user's
#: stored API keys.
_LEGACY_VERIFY_SENTINELS = frozenset({"rikugan-encryption-v1"})
#: Everything :func:`decrypt_keys` recognises.
_ACCEPTED_VERIFY_SENTINELS = _LEGACY_VERIFY_SENTINELS | {_VERIFY_SENTINEL}
_PBKDF2_ITERATIONS = 600_000


def is_available() -> bool:
    """Return True if the ``cryptography`` library is importable.

    Uses :func:`importlib.util.find_spec` so this check is O(1) and does
    not import the (heavy) ``cryptography`` package itself.
    """
    return importlib.util.find_spec("cryptography") is not None


def _load_crypto_primitives() -> tuple[Any, Any, Any, Any]:
    """Import the heavy crypto primitives on first use.

    Returns the tuple ``(AESGCM, SHA256, PBKDF2HMAC, InvalidTag)`` so
    callers can use them locally without relying on module-global names.
    Raises ``ImportError`` if the package is not available.
    """
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    return AESGCM, SHA256, PBKDF2HMAC, InvalidTag


def _derive_key(password: str, salt: bytes) -> bytes:
    """Derive a 32-byte AES key from *password* + *salt*."""
    _AESGCM, SHA256, PBKDF2HMAC, _InvalidTag = _load_crypto_primitives()
    kdf = PBKDF2HMAC(
        algorithm=SHA256(),
        length=32,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    return kdf.derive(password.encode("utf-8"))


def encrypt_keys(password: str, key_data: dict) -> dict:
    """Encrypt *key_data* and return a dict suitable for JSON serialisation.

    Returns ``{"salt": ..., "nonce": ..., "ciphertext": ...}`` where each
    value is a Base-64 encoded string.
    """
    AESGCM, _SHA256, _PBKDF2HMAC, _InvalidTag = _load_crypto_primitives()
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = _derive_key(password, salt)
    plaintext = json.dumps(
        {"verify": _VERIFY_SENTINEL, "keys": key_data},
    ).encode("utf-8")
    ct = AESGCM(key).encrypt(nonce, plaintext, None)
    return {
        "salt": base64.b64encode(salt).decode(),
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(ct).decode(),
    }


def decrypt_keys(password: str, enc_block: dict) -> dict:
    """Decrypt *enc_block* and return the original key-data dict.

    Raises ``ValueError`` on wrong password or corrupted data.
    """
    AESGCM, _SHA256, _PBKDF2HMAC, InvalidTag = _load_crypto_primitives()
    try:
        salt = base64.b64decode(enc_block["salt"])
        nonce = base64.b64decode(enc_block["nonce"])
        ct = base64.b64decode(enc_block["ciphertext"])
    except (KeyError, Exception) as exc:
        raise ValueError("Malformed encryption block") from exc

    key = _derive_key(password, salt)
    try:
        plaintext = AESGCM(key).decrypt(nonce, ct, None)
    except InvalidTag as exc:
        raise ValueError("Wrong password or corrupted data") from exc

    payload = json.loads(plaintext)
    if payload.get("verify") not in _ACCEPTED_VERIFY_SENTINELS:
        raise ValueError("Decryption verification failed")
    return payload["keys"]
