__all__ = ['PALMFormatter', 'AuditRawFormatter']

from typing import Any
from typing import Optional
import json
import logging
from pythonjsonlogger.json import JsonFormatter

try:
    from utils.auth_middleware import get_user
except ModuleNotFoundError:
    from .utils.auth_middleware import get_user

RESERVED_FIELDS = {
    'duration',
    'message',
    'user',
    'level',
    'messagekey',
    'message',
    'exception',
    'stacktrace',
    'timestamp',
    'servicename',
    'component',
    'rest',
    'hostname',
    'version',
    'requestid',
    'confidential',
}

class PALMFormatter(JsonFormatter):
    def get_user_login(self) -> Optional[str]:
        user = get_user()
        if user is not None:
            return user.login

    def process_log_record(self, log_record: dict[str, Any]) -> dict[str, Any]:
        out, extra = {}, {}
        for k, v in log_record.items():
            if k in RESERVED_FIELDS:
                out[k] = v
            else:
                extra[k] = v
        for k, v in extra.items():
            key = k if k.startswith('metadata.') else f'metadata.{k}'
            out[key] = v
        return {k: v for k, v in out.items() if v is not None}

class AuditRawFormatter(logging.Formatter):
    """
    Форматировщик для аудита - выводит сообщение в json,
    игнорируя стандартные поля LogRecord,
    для соблюдения строгого контракта внешних систем аудита.
    """
    def format(self, record: logging.LogRecord) -> str:
        # Если передали словарь в msg, просто сериализуем его
        if isinstance(record.msg, dict):
            return json.dumps(record.msg, ensure_ascii=False)
        # Иначе фоллбэк на стандартное поведение
        return super().format(record)
