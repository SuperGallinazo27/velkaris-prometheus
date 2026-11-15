"""Fail-closed configuration without dotenv or secret values in responses."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from api.security import MAX_PASSWORD_LENGTH, normalize_email

ROOT = Path(__file__).resolve().parents[1]


def _number(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        raise ValueError(f"{name} debe ser un número.") from None


@dataclass(frozen=True)
class Settings:
    environment: str = "development"
    cors_origins: tuple[str, ...] = ()
    dataset_path: Path | None = None
    max_body_bytes: int = 16_384
    max_dataset_bytes: int = 1_048_576
    request_timeout_seconds: float = 35.0
    gee_enabled: bool = False
    gee_project_id: str | None = None
    gee_credentials_path: Path | None = None
    gee_timeout_seconds: float = 25.0
    targets_path: Path | None = None
    # Accounts. The URL and the bootstrap password stay out of repr() and of every error message.
    database_url: str | None = field(default=None, repr=False)
    session_hours: float = 12.0
    temporary_password_days: float = 7.0
    min_password_length: int = 8
    admin_email: str | None = None
    admin_password: str | None = field(default=None, repr=False)
    # Assistant (Gemini). The key stays out of repr(), of every response and of every log line.
    gemini_api_key: str | None = field(default=None, repr=False)
    gemini_model: str = "gemini-3.5-flash"
    gemini_fallback_model: str | None = "gemini-flash-lite-latest"
    assistant_hourly_limit: int = 40

    def __post_init__(self):
        for model in (self.gemini_model, self.gemini_fallback_model):
            # The name becomes part of the upstream URL path.
            if model is not None and not re.fullmatch(r"[a-z0-9][a-z0-9.\-]{0,80}", model):
                raise ValueError("GEMINI_MODEL y GEMINI_FALLBACK_MODEL deben ser nombres de modelo como gemini-3.5-flash.")
        if self.assistant_hourly_limit <= 0:
            raise ValueError("ASSISTANT_HOURLY_LIMIT debe ser positivo.")
        if self.environment not in {"development", "test", "production"}:
            raise ValueError("APP_ENV debe ser development, test o production.")
        for origin in self.cors_origins:
            parsed = urlsplit(origin)
            if ("*" in origin or parsed.scheme not in {"http", "https"}
                    or not parsed.hostname or parsed.username or parsed.password
                    or parsed.path or parsed.query or parsed.fragment):
                raise ValueError("CORS_ORIGINS requiere orígenes exactos, sin rutas ni comodines.")
            if self.environment == "production" and parsed.scheme != "https":
                raise ValueError("CORS de producción requiere HTTPS.")
        if self.max_body_bytes <= 0 or self.max_dataset_bytes <= 0 or self.request_timeout_seconds <= 0:
            raise ValueError("Los límites deben ser positivos.")
        # The upper bounds keep "now + lifetime" inside the datetime range; NaN fails both comparisons.
        if not (0 < self.session_hours <= 8_760 and 0 < self.temporary_password_days <= 366):
            raise ValueError("SESSION_HOURS (máximo 8760) y TEMPORARY_PASSWORD_DAYS (máximo 366) deben ser positivos.")
        if not 1 <= self.min_password_length <= MAX_PASSWORD_LENGTH:
            raise ValueError(f"La longitud mínima de contraseña debe estar entre 1 y {MAX_PASSWORD_LENGTH}.")
        if self.admin_email is not None and normalize_email(self.admin_email) is None:
            raise ValueError("ADMIN_EMAIL no es un correo válido.")
        if (self.admin_password is not None
                and not self.min_password_length <= len(self.admin_password) <= MAX_PASSWORD_LENGTH):
            raise ValueError(f"ADMIN_PASSWORD debe tener entre {self.min_password_length} y {MAX_PASSWORD_LENGTH} caracteres.")

    @classmethod
    def from_env(cls):
        path = os.getenv("PROMETHEUS_DATASET_PATH")
        return cls(
            environment=os.getenv("APP_ENV", "development"),
            cors_origins=tuple(x.strip() for x in os.getenv("CORS_ORIGINS", "").split(",") if x.strip()),
            dataset_path=Path(path) if path else None,
            gee_enabled=os.getenv("GEE_ENABLED", "false").lower() == "true",
            gee_project_id=os.getenv("GEE_PROJECT_ID") or None,
            gee_credentials_path=Path(os.environ["GOOGLE_APPLICATION_CREDENTIALS"]) if os.getenv("GOOGLE_APPLICATION_CREDENTIALS") else None,
            targets_path=Path(os.environ["PROMETHEUS_TARGETS_PATH"]) if os.getenv("PROMETHEUS_TARGETS_PATH") else None,
            database_url=os.getenv("DATABASE_URL", "").strip() or None,
            session_hours=_number("SESSION_HOURS", 12.0),
            temporary_password_days=_number("TEMPORARY_PASSWORD_DAYS", 7.0),
            admin_email=os.getenv("ADMIN_EMAIL", "").strip() or None,
            admin_password=os.getenv("ADMIN_PASSWORD") or None,
            gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip() or None,
            gemini_model=os.getenv("GEMINI_MODEL", "").strip() or "gemini-3.5-flash",
            gemini_fallback_model=os.getenv("GEMINI_FALLBACK_MODEL", "gemini-flash-lite-latest").strip() or None,
            assistant_hourly_limit=int(_number("ASSISTANT_HOURLY_LIMIT", 40)),
        )
