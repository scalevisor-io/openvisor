"""Suite-wide test setup.

The public auth forms sit behind the Altcha proof of work (§captcha), and the
suite drives them with TestClient - there is no browser to burn CPU on a
challenge, and every module that logs in would otherwise have to solve one. So
the gate is off by default here and `test_captcha.py` turns it back on to
exercise it for real, with the difficulty turned down to something a test can
brute-force in milliseconds.
"""
from app.core.config import settings

settings.altcha_enabled = False
settings.altcha_max_number = 2_000


def solve_altcha(client) -> str:
    """Fetch a challenge and solve it, the way the browser widget does."""
    import base64
    import hashlib
    import json

    challenge = client.get("/api/auth/altcha").json()
    salt, target = challenge["salt"], challenge["challenge"]
    for n in range(challenge["maxnumber"] + 1):
        if hashlib.sha256(f"{salt}{n}".encode()).hexdigest() == target:
            return base64.b64encode(json.dumps({
                "algorithm": challenge["algorithm"], "challenge": target,
                "number": n, "salt": salt,
                "signature": challenge["signature"]}).encode()).decode()
    raise AssertionError("the altcha challenge had no solution below maxnumber")
