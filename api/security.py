"""Password hashing, opaque session tokens and login throttling; standard library only."""

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import OrderedDict

# Production cost: 32 MiB and three passes per hash. Tests lower SCRYPT_LOG2_N. Every stored
# value carries its own parameters, so existing hashes keep verifying after a change.
SCRYPT_LOG2_N = 15
SCRYPT_R = 8
SCRYPT_P = 3
SALT_BYTES = 16
KEY_BYTES = 32
MAX_PASSWORD_LENGTH = 128
MAX_EMAIL_LENGTH = 254
TEMPORARY_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/l/I
# scrypt of discarded random bytes at the production cost. It is verified when the account does
# not exist, so login takes the same time either way. Regenerate it if the cost changes.
DUMMY_HASH = "scrypt$15$8$3$q9uSPBuob7piWe881+pbAA==$eLwk1FvB+OAFV/eNamQGcLH1OIzOn3trVaIC3Otc3SM="

_LABEL = r"[^@\s.\x00-\x1f\x7f-\x9f]+"
EMAIL_PATTERN = re.compile(rf"[^@\s\x00-\x1f\x7f-\x9f]+@{_LABEL}(?:\.{_LABEL})+")
# A burst of logins must not allocate 32 MiB per request on a small instance.
_hashing_slots = threading.BoundedSemaphore(2)


def normalize_email(value: str) -> str | None:
    """Trimmed, lower-cased address; None when it does not look like one."""
    email = value.strip().lower()
    return email if len(email) <= MAX_EMAIL_LENGTH and EMAIL_PATTERN.fullmatch(email) else None


def _scrypt(password: str, salt: bytes, log2_n: int, r: int, p: int, length: int) -> bytes:
    n = 1 << log2_n
    with _hashing_slots:
        # OpenSSL needs 128*r*(n+p+2) bytes; its default limit (32 MiB) is just below that.
        return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                              maxmem=256 * r * (n + p + 2), dklen=length)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    key = _scrypt(password, salt, SCRYPT_LOG2_N, SCRYPT_R, SCRYPT_P, KEY_BYTES)
    return "$".join(("scrypt", str(SCRYPT_LOG2_N), str(SCRYPT_R), str(SCRYPT_P),
                     base64.b64encode(salt).decode(), base64.b64encode(key).decode()))


def verify_password(password: str, stored: str | None) -> bool:
    """False for a wrong password, a missing account (None) or a malformed stored value."""
    try:
        scheme, log2_n, r, p, salt, key = (DUMMY_HASH if stored is None else stored).split("$")
        cost = int(log2_n), int(r), int(p)
        expected = base64.b64decode(key, validate=True)
        if scheme != "scrypt" or not (1 <= cost[0] <= 20 and 1 <= cost[1] <= 32 and 1 <= cost[2] <= 16):
            return False
        derived = _scrypt(password, base64.b64decode(salt, validate=True), *cost, len(expected))
    except (ValueError, TypeError, AttributeError, OverflowError, MemoryError):
        return False
    return hmac.compare_digest(derived, expected) and stored is not None


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    """Only this digest is stored: a leaked table cannot be replayed as bearer tokens."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_temporary_password() -> str:
    return "-".join("".join(secrets.choice(TEMPORARY_ALPHABET) for _ in range(4)) for _ in range(3))


class LoginThrottle:
    """Failed logins per (email, client IP), in memory: valid for a single worker process."""

    def __init__(self, attempts: int = 5, window_seconds: float = 900.0, max_keys: int = 10_000,
                 clock=time.monotonic):
        self.attempts, self.window_seconds, self.max_keys, self.clock = attempts, window_seconds, max_keys, clock
        self._failures: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
        self._lock = threading.Lock()

    def reserve(self, key: tuple[str, str]) -> bool:
        """Count an attempt as failed before checking it, so parallel requests cannot exceed the
        limit; False when the key is blocked. clear() or release() undo the count."""
        with self._lock:
            now = self.clock()
            recent = [moment for moment in self._failures.pop(key, ()) if moment > now - self.window_seconds]
            allowed = len(recent) < self.attempts
            if allowed:
                recent.append(now)
            if recent:
                self._failures[key] = recent
            while len(self._failures) > self.max_keys:
                self._failures.popitem(last=False)
            return allowed

    def release(self, key: tuple[str, str]) -> None:
        """The attempt ended without a credential verdict (for example, a database outage)."""
        with self._lock:
            recent = self._failures.get(key)
            if recent:
                recent.pop()
                if not recent:
                    del self._failures[key]

    def clear(self, key: tuple[str, str]) -> None:
        with self._lock:
            self._failures.pop(key, None)
