"""Envelope encryption (PROMPT §13/§15): each project gets a random Fernet data
key; the data key is wrapped by the master key from MASTER_ENCRYPTION_KEY.
Values are stored as: wrapped_data_key ":" ciphertext (both urlsafe base64)."""
import base64
import hashlib

from cryptography.fernet import Fernet

from app.core.config import settings


def _master() -> Fernet:
    # Accept any string master key: derive a proper 32-byte Fernet key from it.
    digest = hashlib.sha256(settings.master_encryption_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value: str) -> str:
    data_key = Fernet.generate_key()
    ciphertext = Fernet(data_key).encrypt(value.encode())
    wrapped = _master().encrypt(data_key)
    return wrapped.decode() + ":" + ciphertext.decode()


def decrypt(blob: str) -> str:
    wrapped, ciphertext = blob.split(":", 1)
    data_key = _master().decrypt(wrapped.encode())
    return Fernet(data_key).decrypt(ciphertext.encode()).decode()
