"""Sessions (signed cookie), passwords, CSRF, misc token helpers."""
import hashlib
import secrets

from itsdangerous import BadSignature, URLSafeTimedSerializer
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

SESSION_COOKIE = "session"
CSRF_COOKIE = "csrf_token"
SESSION_MAX_AGE = 60 * 60 * 24 * 14  # 14 days

_serializer = URLSafeTimedSerializer(settings.secret_key, salt="session")
_action_serializer = URLSafeTimedSerializer(settings.secret_key, salt="action")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    try:
        return pwd_context.verify(password, hashed)
    except Exception:
        return False


def create_session_token(user_id: str) -> str:
    return _serializer.dumps({"uid": user_id})


def read_session_token(token: str) -> str | None:
    try:
        return _serializer.loads(token, max_age=SESSION_MAX_AGE)["uid"]
    except (BadSignature, KeyError):
        return None


def create_action_token(purpose: str, user_id: str) -> str:
    """Email verification / password reset tokens."""
    return _action_serializer.dumps({"p": purpose, "uid": user_id})


def read_action_token(token: str, purpose: str, max_age: int = 60 * 60 * 48) -> str | None:
    try:
        data = _action_serializer.loads(token, max_age=max_age)
        return data["uid"] if data.get("p") == purpose else None
    except (BadSignature, KeyError):
        return None


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def new_api_token() -> tuple[str, str]:
    """Returns (plaintext, sha256 hash) for MCP API tokens. The prefix is
    instance branding on the plaintext only - validation hashes the whole
    string, so existing tokens survive a prefix change."""
    from app.core.config import settings
    tok = settings.token_prefix + secrets.token_urlsafe(32)
    return tok, hashlib.sha256(tok.encode()).hexdigest()


def hash_api_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
