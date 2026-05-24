import os
from socket import gethostname


FLUENTBIT_CONFIG = {
    "host": os.getenv("LOGSTASH_HOST", "palm-monitoring-client-logger-svc").split(':')[0],
    "port": int(os.getenv("LOGSTASH_PORT", "24224")),
}

LOGGER_CONFIG = {
    "app_name": os.getenv("APP_NAME", "agent-tools"),
    "pod_name": gethostname(),
    "pod_namespace": os.getenv("POD_NAMESPACE", ""),
    "log_level": os.getenv("LOG_LEVEL", "INFO"),
    "fluentbit": FLUENTBIT_CONFIG,
}
