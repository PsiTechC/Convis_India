"""
Rate Limiting Middleware for Convis
Prevents abuse and ensures fair usage across all users.

IMPORTANT: behind App Runner / CloudFront / nginx the direct `request.client.host`
returns the proxy's internal address (e.g. 169.254.172.2 for App Runner). We MUST
read X-Forwarded-For to identify the actual visitor — otherwise every request
shares one rate-limit bucket and the per-IP cap caps the entire platform globally
(this bug shipped briefly in production: a 10/hour limit on a public endpoint
locked everyone out after 10 calls total).
"""
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi import Request
from fastapi.responses import JSONResponse
import logging

logger = logging.getLogger(__name__)


def real_client_ip(request: Request) -> str:
    """Extract the visitor's real IP — anti-spoofing aware.

    Trusted-proxy model: AWS App Runner APPENDS the real TCP client IP to
    `X-Forwarded-For` (it does not replace). So the LAST entry of X-F-F is
    the value App Runner itself wrote — trustworthy. EARLIER entries were
    set by the client OR by upstream proxies; both are attacker-controllable
    on a public endpoint and must NEVER be used as a rate-limit identity.

    The earlier (now-fixed) bug: this function returned `xff.split(",")[0]`
    which is the FIRST entry — i.e. whatever value the attacker chose to
    inject. Any client could send `X-Forwarded-For: 1.1.1.1` and get a
    fresh per-IP rate-limit bucket on every request.

    If you put CloudFront in front of App Runner, increment TRUSTED_HOPS to
    2 (CF appends + App Runner appends = 2 trusted entries at the tail).
    """
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        ips = [ip.strip() for ip in fwd.split(",") if ip.strip()]
        if ips:
            # 1 trusted hop: App Runner. The last entry is App Runner's
            # appended real-source IP. If only one entry exists, that's it.
            return ips[-1]
    # Fallback: X-Real-IP (set by some nginx configs)
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    # Last resort: TCP source (only correct when not behind a proxy).
    return get_remote_address(request)


# Create limiter instance — keyed on the REAL client IP (X-Forwarded-For aware).
limiter = Limiter(key_func=real_client_ip, default_limits=["1000/hour"])


def get_user_id_from_request(request: Request) -> str:
    """
    Extract user ID from JWT token for per-user rate limiting
    Falls back to IP address if no user is authenticated
    """
    try:
        # Try to get user from request state (set by auth middleware)
        if hasattr(request.state, "user_id"):
            return f"user:{request.state.user_id}"

        # Fall back to IP address
        return real_client_ip(request)
    except Exception:
        return real_client_ip(request)


# Custom rate limit exceeded handler
async def custom_rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """
    Custom error response for rate limit exceeded
    """
    logger.warning(f"Rate limit exceeded for {real_client_ip(request)}: {exc.detail}")

    return JSONResponse(
        status_code=429,
        content={
            "detail": "Too many requests. Please slow down and try again later.",
            "error": "rate_limit_exceeded",
            "retry_after": exc.detail
        }
    )


# Rate limit configurations for different endpoint types
RATE_LIMITS = {
    # WebSocket endpoints (voice calls) - most critical
    "websocket": "10/minute",  # Max 10 concurrent call initiations per minute

    # API key operations
    "api_key_create": "5/hour",  # Max 5 new API keys per hour

    # Assistant operations
    "assistant_create": "20/hour",  # Max 20 new assistants per hour
    "assistant_update": "60/hour",  # Max 60 updates per hour

    # Call operations
    "outbound_call": "30/minute",  # Max 30 outbound calls per minute
    "call_query": "100/minute",  # Max 100 call log queries per minute

    # File uploads
    "file_upload": "20/hour",  # Max 20 file uploads per hour

    # General API
    "general": "200/minute"  # General API rate limit
}


def get_rate_limit(endpoint_type: str) -> str:
    """Get rate limit string for a specific endpoint type"""
    return RATE_LIMITS.get(endpoint_type, RATE_LIMITS["general"])
