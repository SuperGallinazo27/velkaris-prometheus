"""Fail-closed configuration without dotenv or secret values in responses."""

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


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

    def __post_init__(self):
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
        )
