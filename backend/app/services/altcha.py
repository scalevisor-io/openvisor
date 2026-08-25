"""Self-hosted Altcha proof-of-work captcha (no third party, PROMPT §2).

The server mints a challenge, the browser burns a little CPU finding the number
that hashes to it, and the server checks the work: nothing leaves the instance,
which is the whole reason a hosted captcha is out of the question here.

The scheme (https://altcha.org): the server picks a random `number` below
`maxnumber` and a random `salt`, publishes `sha256(salt + number)` as the
challenge plus an HMAC of it under the server secret. The client iterates
0..maxnumber until it finds the number and returns it. The server re-derives the
hash, checks its own signature (so a challenge it did not issue is worth
nothing), and burns the challenge so a solved one cannot be replayed.
"""
import base64
import hashlib
import hmac
import json
import secrets
import time

from fastapi import HTTPException

from app.core.config import settings
from app.services.events import get_async_redis

CHALLENGE_TTL = 600  # seconds


def _sign(challenge: str) -> str:
    return hmac.new(settings.secret_key.encode(), challenge.encode(), hashlib.sha256).hexdigest()


def create_challenge() -> dict:
    # The expiry rides inside the salt, so issuing a challenge stores nothing
    # and an abandoned one costs nothing.
    salt = f"{secrets.token_hex(12)}?expires={int(time.time()) + CHALLENGE_TTL}"
    # Read at issue time, not at import: the difficulty is a knob
    # (settings.altcha_max_number), and the test suite turns it down.
    max_number = settings.altcha_max_number
    number = secrets.randbelow(max_number)
    challenge = hashlib.sha256(f"{salt}{number}".encode()).hexdigest()
    return {
        "algorithm": "SHA-256",
        "challenge": challenge,
        "maxnumber": max_number,
        "salt": salt,
        "signature": _sign(challenge),
    }


async def verify_payload(payload_b64: str | None) -> bool:
    """Check a solved challenge. False on anything malformed, expired or reused."""
    if not payload_b64:
        return False
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
    # Constant-time, and the reason a client cannot mint its own challenge.
    if not hmac.compare_digest(_sign(challenge), signature):
        return False
    # Single use: a solved challenge is a bearer token for one attempt, and
    # without this one solve buys unlimited retries. SET NX is the whole check -
    # it succeeds exactly once per challenge.
    try:
        r = get_async_redis()
        return bool(await r.set(f"altcha:{challenge}", "1", nx=True, ex=CHALLENGE_TTL))
    except Exception:
        # Redis down. The signature and hash checks already passed, so the work
        # was genuinely done; allowing replay for the TTL beats locking everyone
        # out of signing in during a cache outage.
        return True


async def gate(payload: str | None) -> None:
    """Gate an unauthenticated auth form behind the proof of work.

    Signup and sign-in have to agree about this, so the rule lives here rather
    than in either of them. Deliberately the same error either way: telling a
    script whether its challenge expired, was replayed or was forged only helps
    it.
    """
    if not settings.altcha_enabled:
        return
    if not await verify_payload(payload):
        raise HTTPException(400, "Captcha verification failed. Please try again.")
