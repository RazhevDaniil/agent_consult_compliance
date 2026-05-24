__all__ = ['PALMFormatter']

from typing import Any
from pythonjsonlogger.json import JsonFormatter


RESERVED_FIELDS = {
    'duration',
    'message',
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
