from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Все поля читаются из переменных окружения. BaseSettings автоматически матчит snake_case → UPPER_CASE (например, app_name → APP_NAME)."""

    app_name: str = "app"
    log_level: str = "INFO"
    port: int = 444


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
