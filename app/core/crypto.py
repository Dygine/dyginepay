"""
Encryption at rest for gateway credentials, and password hashing for admins.

Fernet gives authenticated encryption, so a tampered ciphertext fails to decrypt
rather than returning garbage. The key is derived from ENCRYPTION_KEY with a
fixed salt: that is deliberate, because a random salt would need to be stored
alongside every value and we want the same key to read everything already written.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

_ph = PasswordHasher()


def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.ENCRYPTION_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plaintext: str | None) -> str | None:
    if plaintext is None or plaintext == "":
        return None
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str | None) -> str | None:
    if not ciphertext:
        return None
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def is_readable(ciphertext: str | None) -> bool:
    """True when a stored secret can still be decrypted with the current key."""
    return decrypt(ciphertext) is not None


# ----------------------------------------------------------- passwords --
def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        return _ph.verify(stored_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# ---------------------------------------------------------- api tokens --
def generate_secret(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """
    API secrets are hashed with SHA-256, not Argon2.

    That looks wrong until you notice these are 256-bit random tokens, not
    user-chosen passwords. There is no dictionary to attack, so the slow hash
    buys nothing - and it would be run on every single API request.
    """
    return hashlib.sha256(token.encode()).hexdigest()


def tokens_equal(a: str, b: str) -> bool:
    """Constant-time compare, so a timing signal cannot leak the value."""
    return hmac.compare_digest(a, b)


# ------------------------------------------------------------- signing --
def sign_hmac_sha256(secret: str, message: str | bytes) -> str:
    raw = message if isinstance(message, bytes) else message.encode()
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def verify_hmac_sha256(secret: str, message: str | bytes, signature: str) -> bool:
    expected = sign_hmac_sha256(secret, message)
    return hmac.compare_digest(expected, (signature or "").strip())
