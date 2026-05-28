"""
Password hashing, key derivation, and authenticated encryption.

Two layers of keys:
  1. KEK ("key encryption key") — derived from a user's login password via
     Argon2id. Used to wrap/unwrap per-account data keys.
  2. Data key — a random Fernet key per CAS account. Used to encrypt that
     account's PDFs, parse cache, and stored credentials.

Why two layers: if a user changes their password (or another user is granted
access), only the wrapped copies of the data key need re-encrypting — the
underlying CAS files don't have to be rewritten.

Forgetting a password = the data key for that account is unrecoverable, so
that account's data is lost. The user explicitly accepted this tradeoff.
"""
from __future__ import annotations

import base64
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from argon2.low_level import Type, hash_secret_raw
from cryptography.fernet import Fernet, InvalidToken

# Argon2id params: roughly 100ms on a Mac mini. Tune later if needed.
_KDF_TIME_COST = 3
_KDF_MEMORY_COST = 64 * 1024  # 64 MiB
_KDF_PARALLELISM = 4
_KDF_KEY_LEN = 32

_PH = PasswordHasher(
    time_cost=_KDF_TIME_COST,
    memory_cost=_KDF_MEMORY_COST,
    parallelism=_KDF_PARALLELISM,
)


def hash_password(password: str) -> str:
    """Return an Argon2id hash string (salt embedded) for password verification."""
    return _PH.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    try:
        _PH.verify(stored_hash, password)
        return True
    except VerifyMismatchError:
        return False
    except Exception:
        return False


def new_kek_salt() -> bytes:
    return secrets.token_bytes(16)


def derive_kek(password: str, salt: bytes) -> bytes:
    """Derive a 32-byte KEK from password + salt, base64-encoded for Fernet."""
    raw = hash_secret_raw(
        secret=password.encode("utf-8"),
        salt=salt,
        time_cost=_KDF_TIME_COST,
        memory_cost=_KDF_MEMORY_COST,
        parallelism=_KDF_PARALLELISM,
        hash_len=_KDF_KEY_LEN,
        type=Type.ID,
    )
    return base64.urlsafe_b64encode(raw)


def new_data_key() -> bytes:
    """A fresh Fernet key for encrypting one CAS account's data."""
    return Fernet.generate_key()


def wrap_key(data_key: bytes, kek: bytes) -> bytes:
    """Encrypt a data key with a KEK so it can be stored at rest."""
    return Fernet(kek).encrypt(data_key)


def unwrap_key(wrapped: bytes, kek: bytes) -> bytes:
    """Reverse wrap_key. Raises InvalidToken on wrong KEK."""
    return Fernet(kek).decrypt(wrapped)


def encrypt_bytes(plaintext: bytes, data_key: bytes) -> bytes:
    return Fernet(data_key).encrypt(plaintext)


def decrypt_bytes(ciphertext: bytes, data_key: bytes) -> bytes:
    return Fernet(data_key).decrypt(ciphertext)


def encrypt_str(plaintext: str, data_key: bytes) -> bytes:
    return encrypt_bytes(plaintext.encode("utf-8"), data_key)


def decrypt_str(ciphertext: bytes, data_key: bytes) -> str:
    return decrypt_bytes(ciphertext, data_key).decode("utf-8")


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def new_invite_token() -> str:
    return secrets.token_urlsafe(24)


__all__ = [
    "InvalidToken",
    "decrypt_bytes",
    "decrypt_str",
    "derive_kek",
    "encrypt_bytes",
    "encrypt_str",
    "hash_password",
    "new_data_key",
    "new_invite_token",
    "new_kek_salt",
    "new_session_token",
    "unwrap_key",
    "verify_password",
    "wrap_key",
]
