import os

from pydantic import BaseModel, SecretStr, Field
from pydantic_settings import BaseSettings, TomlConfigSettingsSource
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ..logging_config import get_logger

log = get_logger("app.session")

config_file: str | None = os.environ.get("DB_SECRETS")


class Database(BaseModel):
    """Connection settings for a single database."""

    dialect: str | None = Field(default="")
    username: str | None = Field(default="")
    password: SecretStr | None = Field(default="")
    host: str | None = Field(default="")
    port: str | None = Field(default="")
    database: str | None = Field(default="")
    query: str | None = Field(default="")
    url: str | None = Field(default="")


class Connections(BaseModel):
    """All database connections."""

    model_config = {"populate_by_name": True}

    db1: Database = Field(alias="deal-agent-db")


class ConnectionsConfig(BaseSettings):
    """Database settings loaded from a TOML file pointed to by DB_SECRETS."""

    connections: Connections

    @classmethod
    def settings_customise_sources(cls, settings_cls, **kwargs):
        return (TomlConfigSettingsSource(settings_cls, toml_file=config_file),)


def _build_url(db: Database) -> str:
    if db.url:
        return db.url
    query = "" if db.query is None else f"?{db.query}"
    password = db.password.get_secret_value() if db.password else ""
    return f"{db.dialect}://{db.username}:{password}@{db.host}:{db.port}/{db.database}{query}"


def _create_engine(db: Database) -> AsyncEngine:
    url = _build_url(db)
    echo = os.getenv("LOG_LEVEL", "INFO") == "DEBUG"
    kwargs: dict = {"echo": echo, "echo_pool": echo}
    if "sqlite" not in url:
        kwargs.update(
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
            pool_timeout=30,
            pool_recycle=1800,
            # connect_args={
            #     "server_settings": {
            #         "search_path": '"aif-agentslab-acdc-dep-post-service-db",public',
            #     }
            # },
        )
    return create_async_engine(url, **kwargs)


cfg = ConnectionsConfig()
engine = _create_engine(cfg.connections.db1)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

log.info("db_engine_created", url=str(engine.url))
