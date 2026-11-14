import hmac
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from secrets import token_urlsafe

import jwt
from pwdlib import PasswordHash
from starlette.responses import Response

from app.core.config import settings

# Argon2 is deliberately slow and memory-hard so leaked hashes are costly to crack.
password_hash = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, hashed_password: str) -> bool:
    return password_hash.verify(password, hashed_password)


def create_access_token(user_id: int) -> str:
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=settings.access_token_minutes)
    return jwt.encode(
        {"sub": str(user_id), "iat": now, "exp": expires_at},
        settings.jwt_secret.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def decode_access_token(token: str) -> int | None:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
        )
        subject = payload.get("sub")
        return int(subject) if isinstance(subject, str) else None
    except (jwt.InvalidTokenError, ValueError):
        return None


def new_csrf_token() -> str:
    nonce = token_urlsafe(32)
    # Signing prevents attackers from planting a CSRF cookie through a sibling subdomain.
    signing_key = hmac.new(
        settings.jwt_secret.get_secret_value().encode(),
        b"parakh-csrf-v1",
        sha256,
    ).digest()
    signature = hmac.new(
        signing_key,
        nonce.encode(),
        sha256,
    ).hexdigest()
    return f"{nonce}.{signature}"


def verify_csrf_token(token: str) -> bool:
    try:
        nonce, signature = token.rsplit(".", 1)
    except ValueError:
        return False
    signing_key = hmac.new(
        settings.jwt_secret.get_secret_value().encode(),
        b"parakh-csrf-v1",
        sha256,
    ).digest()
    expected_signature = hmac.new(
        signing_key,
        nonce.encode(),
        sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, expected_signature)


def set_auth_cookies(response: Response, access_token: str) -> None:
    # Keep JWTs out of JavaScript; SameSite is defense in depth alongside signed CSRF form tokens.
    max_age = settings.access_token_minutes * 60
    response.set_cookie(
        "parakh_access_token",
        access_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=max_age,
        path="/",
    )
    response.set_cookie(
        "parakh_csrf_token",
        new_csrf_token(),
        httponly=False,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=max_age,
        path="/",
    )
