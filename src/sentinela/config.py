from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Profile(BaseModel):
    city: str = "Rio Branco"
    state: str = "AC"
    country: str = "Brazil"
    timezone: str = "America/Rio_Branco"


class Workload(BaseModel):
    ideal_max_weekly_hours: float = 20
    acceptable_max_weekly_hours: float = 30


class Monitoring(BaseModel):
    primary_local_time: str = "07:17"
    backup_local_time: str = "08:17"
    source_timeout_seconds: float = 25
    max_attempts: int = Field(default=3, ge=1, le=5)
    minimum_request_interval_seconds: float = 1.5
    max_document_bytes: int = 12 * 1024 * 1024
    max_pdf_pages: int = 180
    max_ocr_pages: int = 12
    max_documents_per_run: int = 160
    max_run_minutes: int = 40
    circuit_failure_threshold: int = 3
    circuit_cooldown_hours: int = 6
    issue_failure_threshold: int = 6
    heartbeat_stale_minutes: int = 55
    watchdog_hours: int = 30
    freshness_days: int = 90
    # Rendering is slow, so it stays a last resort with a hard budget per run.
    browser_enabled: bool = True
    browser_max_pages: int = 12
    browser_timeout_seconds: float = 45
    registration_reminders: list[int] = [7, 3, 1]
    exam_reminders: list[int] = [14, 7, 3, 1]


class Configuration(BaseModel):
    profile: Profile = Field(default_factory=Profile)
    education: dict[str, list[str]] = {"accepted": ["high_school"]}
    workload: Workload = Field(default_factory=Workload)
    monitoring: Monitoring = Field(default_factory=Monitoring)
    notifications: dict[str, Any] = {"telegram": True, "markdown": True}
    storage: dict[str, Any] = {"warning_size_mb": 350, "reports_directory": "reports"}
    llm: dict[str, Any] = {"enabled": False, "prompt_version": "edital_extraction_v1"}

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.profile.timezone)


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)
    database_url: SecretStr = SecretStr("")
    test_database_url: SecretStr = SecretStr("")
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: SecretStr = SecretStr("")
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    email_from: str = ""
    email_to: str = ""
    llm_base_url: str = ""
    llm_api_key: SecretStr = SecretStr("")
    github_token: SecretStr = SecretStr("")
    github_repository: str = ""
    backup_encryption_key: SecretStr = SecretStr("")
    watchdog_ping_url: SecretStr = SecretStr("")


def load_config(path: str | Path = "config.yaml") -> Configuration:
    return Configuration.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
