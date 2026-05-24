import os
from socket import gethostname


SUBTYPE_ID = "C0"
TYPE_ID = "Audit"
OBJECT_ID = 0
OBJECT_NAME = "agent-ui"


FLUENTBIT_CONFIG = {
    "host": os.getenv("LOGSTASH_HOST", "palm-monitoring-client-logger-svc").split(':')[0],
    "port": int(os.getenv("LOGSTASH_PORT", "24224")),
}

# Отдельный поток для аудита
FLUENTBIT_AUDIT_CONFIG = {
    "host": os.getenv("LOGSTASH_HOST", "palm-monitoring-client-logger-svc").split(':')[0],
    "port": int(os.getenv("AUDIT_LOG_SERVICE_PORT", "24225")),
}

LOGGER_CONFIG = {
    "audit_process_name": os.getenv("AUDIT_PROCESS_NAME", "FP. Вспомогательные инструменты ценообразования"),
    "audit_app_id": os.getenv("AUDIT_APP_ID", "PALM_PSS-CI06054979-CI06054979-202503251222380648"),
    "app_name": os.getenv("APP_NAME", "agent-ui"),
    "pod_name": os.getenv("POD_NAME", gethostname()),
    "pod_namespace": os.getenv("POD_NAMESPACE", ""),
    "log_level": os.getenv("LOG_LEVEL", "INFO"),
    "fluentbit": FLUENTBIT_CONFIG,
    "fluentbit_audit": FLUENTBIT_AUDIT_CONFIG,
}
