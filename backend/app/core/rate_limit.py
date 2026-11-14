from collections import deque
from threading import Lock
from time import monotonic

from fastapi import Request
from fastapi.responses import JSONResponse

WINDOW_SECONDS = 60
# Redis will share this modest credential-endpoint limit across workers in a later phase.
MAX_AUTH_REQUESTS_PER_WINDOW = 20
_buckets: dict[str, deque[float]] = {}
_last_cleanup = 0.0
_lock = Lock()

AUTH_PATHS = {
    "/api/v1/auth/register",
    "/api/v1/auth/login",
    "/register",
    "/login",
}


async def rate_limit_auth_requests(request: Request, call_next):
    global _last_cleanup

    if request.method == "POST" and request.url.path in AUTH_PATHS:
        # Do not trust X-Forwarded-For unless a known proxy is explicitly configured.
        client_ip = request.client.host if request.client else "unknown"
        now = monotonic()
        with _lock:
            if now - _last_cleanup >= WINDOW_SECONDS:
                expired_before = now - WINDOW_SECONDS
                for ip, timestamps in list(_buckets.items()):
                    while timestamps and timestamps[0] <= expired_before:
                        timestamps.popleft()
                    if not timestamps:
                        del _buckets[ip]
                _last_cleanup = now

            timestamps = _buckets.setdefault(client_ip, deque())
            while timestamps and timestamps[0] <= now - WINDOW_SECONDS:
                timestamps.popleft()
            if len(timestamps) >= MAX_AUTH_REQUESTS_PER_WINDOW:
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": {
                            "code": "rate_limited",
                            "message": "Too many authentication attempts. Try again shortly.",
                        }
                    },
                    headers={"Retry-After": str(WINDOW_SECONDS)},
                )
            timestamps.append(now)

    return await call_next(request)
