"""Подключение к Postgres для agent-tools / deal_service_app.

Конфиг:
- DEAL_DB_URL            -> полный override URL для тестов
- DEAL_DB_JDBC_URL       -> jdbc:postgresql://host:port/db[?params]
- DEAL_DB_SCHEMA         -> схема БД, например pss
- VAULT_FILE_PATH        -> путь до /vault/secrets/secrets.properties
"""

from __future__ import annotations

import os
import ssl
from dataclasses import dataclass
from pathlib import Path
from functools import lru_cache
from urllib.parse import parse_qsl, urlparse

import pg8000
import pg8000.dbapi as pg8000_dbapi

from sqlalchemy import URL, Engine, create_engine, text
from sqlalchemy.pool import StaticPool


URL_OVERRIDE = os.getenv("DEAL_DB_URL")  # тестовый override, например sqlite://
JDBC_URL = os.getenv("DEAL_DB_JDBC_URL")
PG_SCHEMA = os.getenv("DEAL_DB_SCHEMA", "pss")
VAULT_FILE_PATH = os.getenv("VAULT_FILE_PATH", "/vault/secrets/secrets.properties")
DB_CONNECT_TIMEOUT = float(os.getenv("DEAL_DB_CONNECT_TIMEOUT", "10"))

TABLE_NAMES: dict[str, str] = {
    "etc": "etc",
    "option_price_nso": "option_price_nso",
    "option_price_depo": "option_price_depo",
    "crl": "crl",
    "eva_pahom": "eva_pahom",
    "min_eva": "min_eva",
    "key_rate": "key_rate",
    "key_rate_spreads": "key_rate_spreads",
    "foreign_limit_rates": "foreign_limit_rates",
    "rub_limit_rates": "rub_limit_rates",
    "history_ftp_spreads_matrix": "history_ftp_spreads_matrix",
}


@dataclass(frozen=True)
class HostPort:
    host: str
    port: int | None = None


@dataclass(frozen=True)
class JdbcPostgresConfig:
    database: str
    hosts: tuple[HostPort, ...]
    query: dict[str, str]


def load_properties(path: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            props[key.strip()] = value.strip()
    return props


def _get_pg_credentials() -> tuple[str, str]:
    props = load_properties(VAULT_FILE_PATH)

    try:
        username = props["pssdb.datasource.username"]
        password = props["pssdb.datasource.password"]
    except KeyError as exc:
        raise RuntimeError(
            f"Postgres credentials not found in {VAULT_FILE_PATH}: {exc}"
        ) from exc

    return username, password


def _parse_host_ports(netloc: str) -> tuple[HostPort, ...]:
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]

    hosts: list[HostPort] = []
    for raw_item in netloc.split(","):
        item = raw_item.strip()
        if not item:
            continue

        host: str
        port: int | None = None

        if item.startswith("[") and "]" in item:
            host, _, tail = item[1:].partition("]")
            if tail.startswith(":"):
                port = int(tail[1:])
        else:
            host_part, sep, port_part = item.rpartition(":")
            if sep and port_part.isdigit():
                host = host_part
                port = int(port_part)
            else:
                host = item

        host = host.strip()
        if not host:
            raise ValueError(f"Host is missing in JDBC URL netloc: {netloc!r}")
        hosts.append(HostPort(host=host, port=port))

    if not hosts:
        raise ValueError(f"No PostgreSQL hosts found in JDBC URL netloc: {netloc!r}")

    return tuple(hosts)


def _jdbc_to_config(jdbc_url: str) -> JdbcPostgresConfig:
    if not jdbc_url.startswith("jdbc:"):
        raise ValueError(f"Expected JDBC URL, got: {jdbc_url!r}")

    # jdbc:postgresql://host:port/database?param=value
    parsed = urlparse(jdbc_url[len("jdbc:"):])

    if parsed.scheme != "postgresql":
        raise ValueError(f"Only PostgreSQL JDBC URL is supported, got: {jdbc_url!r}")

    database = parsed.path.lstrip("/")
    if not database:
        raise ValueError(f"Database name is missing in JDBC URL: {jdbc_url!r}")

    return JdbcPostgresConfig(
        database=database,
        hosts=_parse_host_ports(parsed.netloc),
        query=dict(parse_qsl(parsed.query, keep_blank_values=True)),
    )


def _query_for_pg8000(query: dict[str, str]) -> dict[str, str]:
    jdbc_only = {"preparethreshold", "targetservertype"}
    return {
        key: value
        for key, value in query.items()
        if key.lower() not in jdbc_only
    }


def _target_server_type(query: dict[str, str]) -> str:
    for key, value in query.items():
        if key.lower() == "targetservertype":
            return value.strip().lower()
    return ""


def _config_to_sqlalchemy_url(config: JdbcPostgresConfig, username: str, password: str) -> URL:
    if len(config.hosts) != 1:
        raise ValueError("Multi-host JDBC URL requires multi-host engine creation")

    host_port = config.hosts[0]
    return URL.create(
        drivername="postgresql+pg8000", # drivername="postgresql+psycopg2",
        username=username,
        password=password,
        host=host_port.host,
        port=host_port.port,
        database=config.database,
        query=_query_for_pg8000(config.query),
    )


def _create_engine_from_url(url: str | URL) -> Engine:
    echo = os.getenv("LOG_LEVEL", "INFO").upper() == "DEBUG"

    is_sqlite = isinstance(url, str) and url.startswith("sqlite")
    if is_sqlite:
        # Для тестов с in-memory sqlite и многопоточными клиентами.
        return create_engine(
            url,
            echo=echo,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )

    engine = create_engine(
        url,
        echo=echo,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        pool_timeout=30,
        pool_recycle=1800,
    )
    _suppress_pg8000_close_noise(engine)
    return engine


def _safe_close_dbapi_connection(dbapi_connection) -> None:
    try:
        dbapi_connection.close()
    except Exception as exc:
        if not _is_pg8000_close_noise(exc):
            raise


def _matches_target_server_type(dbapi_connection, target_server_type: str) -> bool:
    target = (target_server_type or "").lower()
    if target not in {"master", "primary", "read-write"}:
        return True

    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SELECT pg_is_in_recovery()")
        row = cursor.fetchone()
        return bool(row) and row[0] is False
    finally:
        cursor.close()


def _create_engine_from_jdbc_config(
        config: JdbcPostgresConfig,
        username: str,
        password: str,
) -> Engine:
    echo = os.getenv("LOG_LEVEL", "INFO").upper() == "DEBUG"
    target_server_type = _target_server_type(config.query)

    def _connect():
        last_error: Exception | None = None

        for host_port in config.hosts:
            dbapi_connection = None
            try:
                dbapi_connection = pg8000_dbapi.connect(
                    user=username,
                    password=password,
                    host=host_port.host,
                    port=host_port.port or 5432,
                    database=config.database,
                    timeout=DB_CONNECT_TIMEOUT,
                )
                if _matches_target_server_type(dbapi_connection, target_server_type):
                    return dbapi_connection

                _safe_close_dbapi_connection(dbapi_connection)
                dbapi_connection = None
                last_error = RuntimeError(
                    f"PostgreSQL host {host_port.host}:{host_port.port or 5432} "
                    f"does not match targetServerType={target_server_type!r}"
                )
            except Exception as exc:
                if dbapi_connection is not None:
                    _safe_close_dbapi_connection(dbapi_connection)
                last_error = exc

        raise RuntimeError(
            "Could not connect to any PostgreSQL host from DEAL_DB_JDBC_URL"
        ) from last_error

    engine = create_engine(
        URL.create(drivername="postgresql+pg8000", database=config.database),
        creator=_connect,
        echo=echo,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        pool_timeout=30,
        pool_recycle=1800,
    )
    _suppress_pg8000_close_noise(engine)
    return engine


def _iter_exception_chain(exc: BaseException):
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _is_pg8000_close_noise(exc: BaseException) -> bool:
    for item in _iter_exception_chain(exc):
        if isinstance(item, (pg8000.exceptions.InterfaceError, ssl.SSLError, OSError)):
            text = f"{item!r} {item}".lower()
            if (
                    "network error" in text
                    or "eof occurred in violation of protocol" in text
                    or "ssleoferror" in text
                    or "connection reset" in text
                    or "connection refused" in text
                    or "broken pipe" in text
            ):
                return True
    return False


def _suppress_pg8000_close_noise(engine: Engine) -> None:
    for attr_name in ("do_close", "do_terminate"):
        original = getattr(engine.dialect, attr_name, None)
        if original is None or getattr(original, "_pss_pg8000_close_noise_wrapped", False):
            continue

        def _wrapped(dbapi_connection, _original=original):
            try:
                return _original(dbapi_connection)
            except Exception as exc:
                if _is_pg8000_close_noise(exc):
                    return None
                raise

        _wrapped._pss_pg8000_close_noise_wrapped = True
        setattr(engine.dialect, attr_name, _wrapped)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    if URL_OVERRIDE:
        return _create_engine_from_url(URL_OVERRIDE)

    if not JDBC_URL:
        raise RuntimeError("DEAL_DB_JDBC_URL is not set")

    username, password = _get_pg_credentials()
    config = _jdbc_to_config(JDBC_URL)
    if len(config.hosts) > 1 or _target_server_type(config.query):
        return _create_engine_from_jdbc_config(config, username, password)

    sa_url = _config_to_sqlalchemy_url(config, username, password)
    return _create_engine_from_url(sa_url)


def get_pg_schema() -> str:
    return PG_SCHEMA


def _qualified_table_name(name: str) -> str:
    if name not in TABLE_NAMES.values():
        raise ValueError(f"Unknown table: {name}")
    return f'"{PG_SCHEMA}"."{name}"'


def truncate_table(name: str, engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE TABLE {_qualified_table_name(name)}"))


def drop_table(name: str, engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {_qualified_table_name(name)}"))


engine: Engine = get_engine()
