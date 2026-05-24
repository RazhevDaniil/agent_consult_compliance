from logging.config import dictConfig
from typing import Any, Union
import logging

try:
    from utils.log_settings import LOGGER_CONFIG
except ModuleNotFoundError:
    from .utils.log_settings import LOGGER_CONFIG

try:
    from utils.handlers import FluentBitHandler
except ModuleNotFoundError:
    from .utils.handlers import FluentBitHandler

try:
    from utils.formatters import PALMFormatter, AuditRawFormatter
except ModuleNotFoundError:
    from .utils.formatters import PALMFormatter, AuditRawFormatter

logger = logging.getLogger(__name__)
_LOGGING_MODE_LOGGED = False


def get_config(settings: dict[str, Union[str, dict[str, str]]]) -> dict[str, Any]:
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "console": {
                "format": "%(asctime)s %(levelname)s %(message)s (%(pathname)s:%(lineno)s)",
            },
            # Стандартный JSON форматтер для логов приложения
            "json": {
                "()": PALMFormatter,
                "format": (
                    "%(asctime)s %(levelname)s %(message)s %(exc_info)s %(stack_info)s "
                    "%(name)s %(process)s %(pathname)s %(lineno)d %(user)s %(requestid)s"
                ),
                "static_fields": {
                    "servicename": settings.get("app_name"),
                    "version": settings.get("app_version"),
                    "hostname": settings.get("pod_name"),
                    "metadata.namespace": settings.get("pod_namespace"),
                    "confidential": "false",
                },
                "rename_fields": {
                    "asctime": "timestamp",
                    "levelname": "level",
                    "name": "component",
                    "exc_info": "exception",
                    "stack_info": "stacktrace",
                },
            },
            # Специфичный форматтер для аудита (Raw JSON)
            "audit_raw": {
                "()": AuditRawFormatter,
            }
        },
        "handlers": {
            "console": {
                "formatter": "json",
                "class": "logging.StreamHandler",
            },
            # обычный поток логов
            "palm_monitoring": {
                "formatter": "json",
                "class": FluentBitHandler,
                "host": settings.get("fluentbit", {}).get("host"),
                "port": settings.get("fluentbit", {}).get("port"),
            },
            # отдельный поток для аудита
            "palm_monitoring_audit": {
                "formatter": "audit_raw",  # Используем Raw форматтер
                "class": FluentBitHandler,
                "host": settings.get("fluentbit_audit", {}).get("host"),
                "port": settings.get("fluentbit_audit", {}).get("port"),
            },
        },
        "loggers": {
            # общий логгер приложения
            "root": {
                "level": settings.get("log_level"),
                "handlers": ["console", "palm_monitoring"],
                "propagate": False,
            },
            # отдельный логгер для аудита
            "audit": {
                "level": "INFO", # Аудит всегда INFO
                "handlers": ["console", "palm_monitoring_audit"],
                "propagate": False,
            },
        },
    }


def _log_logging_mode_once(settings: dict[str, Union[str, dict[str, str]]]) -> None:
    """
    Один раз логируем, как настроено логирование.
    """
    global _LOGGING_MODE_LOGGED
    if _LOGGING_MODE_LOGGED:
        return

    _LOGGING_MODE_LOGGED = True

    app_name = settings.get("app_name")
    app_version = settings.get("app_version")
    pod_name = settings.get("pod_name")
    log_level = settings.get("log_level")

    fluent = settings.get("fluentbit", {}) or {}
    fb_host = fluent.get("host")

    fluent_audit = settings.get("fluentbit_audit", {}) or {}
    fb_audit_host = fluent_audit.get("host")

    logger.info(
        "Logging configured: app_name=%r, pod=%r, level=%r, fb_host=%r, audit_host=%r",
        app_name, pod_name, log_level, fb_host, fb_audit_host
    )


def configure_logging() -> dict[str, Any]:
    """
    Инициализирует logging по централизованной конфигурации.
    """
    settings = LOGGER_CONFIG
    config_data = get_config(settings)
    dictConfig(config_data)

    _log_logging_mode_once(settings)

    return config_data
