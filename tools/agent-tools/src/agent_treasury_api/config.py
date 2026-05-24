"""Централизованная конфигурация deal_service_app."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


_APP_DIR = Path(__file__).parent


class AppSettings(BaseSettings):
    """Корневые настройки приложения.

    Все переменные окружения читаются с префиксом ``DEAL_``.
    Например, поле ``is_local`` соответствует ``DEAL_IS_LOCAL``.
    """

    model_config = SettingsConfigDict(env_prefix="DEAL_")

    # ------------------------------------------------------------------
    # Fallback-значения при отсутствии данных
    # ------------------------------------------------------------------
    key_rate_fallback: float = Field(
        0.15,
        description="Fallback ключевой ставки ЦБ (доля, 0.15 = 15%)",
    )
    nor_fallback: float = Field(
        0.045,
        description="Fallback NOR (доля, 0.045 = 4.5%)",
    )
    crl_fallback: float = Field(
        0.0,
        description="Fallback CRL (0.0 = нет поправки на ликвидность)",
    )
    option_price_fallback: float = Field(
        0.0,
        description="Fallback цены опциона (0.0 = премия не учтена)",
    )
    key_rate_spreads_fallback: float = Field(
        0.0,
        description="Fallback спреда к КС (0.0 = без поправки)",
    )


settings = AppSettings()
