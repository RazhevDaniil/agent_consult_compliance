import os
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')

    app_name: str = 'treasury-supervisor'
    app_version: str = '0.1.0'

    agent_alias: str = Field(
        default='treasury-supervisor',
        description='Алиас агента для маршрутизации в оркестраторе',
    )

    app_host: str = os.getenv("APP_HOST", "0.0.0.0")
    app_port: int = int(os.getenv("APP_PORT", "8082"))

    downstream_depo_agent_url: str = Field(
        default='http://agent-v1.ci09529287-aif-agnbf-dt-kmhelp-corpdep-dev.apps.a4x8eda3.k8s.delta.sbrf.ru',
        description='HTTP endpoint агента по СЮЛ',
    )
    treasury_supervisor_url: str = Field(
        default='http://proxy-v1.ci09529287-aif-agnbf-dt-kmhelp-corpdep-dev.apps.a4x8eda3.k8s.delta.sbrf.ru/api/v1/orchestrator',
        description='Публичный URL JSON-RPC endpoint proxy-сервиса',
    )
    downstream_timeout_seconds: float = 60.0
    blocking_wait_timeout_seconds: float = 120.0
    tasks_get_long_poll_seconds: float = 25.0
    task_ttl_seconds: int = 3600


settings = Settings()
