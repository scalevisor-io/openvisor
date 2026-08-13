"""Self-hosted Altcha proof-of-work captcha (no third party, PROMPT §2)."""
import base64
import hashlib
import hmac
import json
import secrets
import time

from app.services.events import get_async_redis
from app.core.config import settings

MAX_NUMBER = 100_000
CHALLENGE_TTL = 600  # seconds


def _sign(challenge: str) -> str:
    return hmac.new(settings.secret_key.encode(), challenge.encode(), hashlib.sha256).hexdigest()


def create_challenge() -> dict:
    salt = f"{secrets.token_hex(12)}?expires={int(time.time()) + CHALLENGE_TTL}"
    number = secrets.randbelow(MAX_NUMBER)
    challenge = hashlib.sha256(f"{salt}{number}".encode()).hexdigest()
    return {
        "algorithm": "SHA-256",
        "challenge": challenge,
        "maxnumber": MAX_NUMBER,
        "salt": salt,
        "signature": _sign(challenge),
    }


async def verify_payload(payload_b64: str) -> bool:
    try:
        data = json.loads(base64.b64decode(payload_b64))
        salt, number, challenge, signature = (
            data["salt"], int(data["number"]), data["challenge"], data["signature"])
    except Exception:
        return False
    # expiry embedded in salt
    if "?expires=" in salt:
        try:
            if int(salt.split("?expires=")[1]) < time.time():
                return False
        except ValueError:
            return False
    if hashlib.sha256(f"{salt}{number}".encode()).hexdigest() != challenge:
        return False
    if not hmac.compare_digest(_sign(challenge), signature):
        return False
    # replay protection
    r = get_async_redis()
    if not await r.set(f"altcha:{challenge}", "1", nx=True, ex=CHALLENGE_TTL):
        return False
    return True
