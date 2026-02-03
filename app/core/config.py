"""Application configuration using Pydantic Settings."""

from pydantic import PostgresDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # Ignore extra fields in .env
    )

    # Application
    app_name: str = "Articence PBX Microservice"
    app_env: str = "development"
    log_level: str = "INFO"
    api_v1_prefix: str = "/v1"
    debug: bool = True
    host: str = "0.0.0.0"
    port: int = 8000

    # Database
    database_url: PostgresDsn
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_pool_timeout: int = 30
    db_pool_recycle: int = 3600  # 1 hour

    # AI Service
    ai_service_url: str
    ai_service_timeout: int = 30
    ai_service_max_retries: int = 5

    # WebSocket
    ws_heartbeat_interval: int = 30

    # CORS
    cors_origins: str = '["http://localhost:3000","http://localhost:8000"]'

    # Performance
    max_concurrent_ai_tasks: int = 50


settings = Settings()
