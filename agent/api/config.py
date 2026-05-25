import os
import re
import logging
from dotenv import load_dotenv
from functools import lru_cache
from typing import ClassVar, Optional, Annotated

from pydantic import AliasChoices, Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, NoDecode

_LOGGER = logging.getLogger(__name__)

load_dotenv()
_LOGGER.info("--- envs've been successfully loaded ---")

TIMEOUT = 300

EMERGENCY_EMAIL = "FCBusinessSupportTeam@sberbank.ru"
ERROR_TEXT = f"Кажется я сломался, но скоро все починим! Если необходима помощь, обратитесь в поддержку {EMERGENCY_EMAIL}"
TTL_EXCEEDED_TEXT = "Извините, ответ агента занимает слишком много времени. Пожалуйста, попробуйте написать ещё раз."
GIGAPLATFORM_REJECTED_TEXT = (
    "GigaChat временно недоступен по ограничению платформы. "
    "Пожалуйста, повторите запрос позже."
)

ERROR_KPK_LIMIT_TEXT = """Если у вас остались вопросы по расчету лимита, можно воспользоваться следующим шаблоном в СберДруг:
Не работает АС/ПО --> АС "ЕФС.Наш бизнес" --> 02. Возникла ошибка при/после проведения штатной операции --> 09. Вопросы по блоку "Расчет цены" --> 2. Депозиты --> 1. Лимиты КПК/ТБ по депозитам. При создании обращения необходимо указывать номер КПК (8 знаков) и подробно описать проблему, а также детали, в т.ч. идентификаторы клиентов (ИНН). Это существенно ускорит предоставление ответа на запрос."""

_AUTH_HEADER_NAME = "Redirect-Authorization"
_TRACE_HEADER_NAME = "x-trace-id"

DB_APP_TIMEOUT_SEC = 2.0
MAX_PREVIEW_RATIO = 0.05


class AppSettings(BaseSettings):

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_host: str = "0.0.0.0"
    app_port: int = 8081

    palm_security_api_url: str = Field(
        ...,
        description="Base URL of PALM.Security; '/agent-tools' is appended for ADAPTERS_API_BASE_URL.",
    )
    db_app_url: str = Field(..., description="Base URL of the db_app service.")

    strict_serialization: bool = True

    # === Service metadata ===
    version: str = "1.0.0"
    title: str = "RAG depo pricing methodology"

    # === graph TTL + retry ===
    graph_timeout_sec: int = 600
    graph_max_retries: int = 2
    graph_retry_backoff_base: float = 0.6
    agent_hops_limit: int = 20

    # === LLM retry ===
    http_max_retries: int = 3
    http_retry_base: float = 0.5
    http_retry_max: float = 5.0
    llm_max_retries: int = 3
    llm_retry_base: float = 0.5
    llm_retry_max: float = 5.0

    # === Logging / docs / retriever ===
    log_level: str = "INFO"
    docs_path: str = "md_docs/"
    chroma_path: str = "/tmp/chroma_bd_api/"
    glossary_path: str = "md_glossary/"
    retriever_main_search_type: str = "mmr"
    retriever_main_fetch_k: int = 10
    retriever_main_lambda_mult: float = 0.2
    context_max_chars: int = 6000
    max_retries: int = 1
    self_confidence_treshold: int = 7
    auto_crit_conf_threshold: int = 7
    num_of_base_vectors: int = 20
    chunk_size: int = 800
    chunk_overlap: int = 150
    summarization_treshold: int = 10
    summarization_window: int = 6
    max_user_chars: int = 32000

    # === KPK pipeline limits ===
    max_chat_history_for_parse: int = 2
    max_chars_per_history_msg: int = 999999
    max_deals_table_rows: int = 999
    max_all_kpk_rows: int = 999

    # === Test-only override for "today" in KPK ===
    kpk_today_override: Optional[str] = None

    # readiness probe ===
    readiness_recheck_interval_sec: int = 3600
    readiness_gigachat_timeout_sec: float = 7200
    readiness_rag_timeout_sec: float = 14400
    readiness_missing_data_timeout_sec: float = 14400

    # === AEF Tracing SDK (SECURITY §18/§20/§21/§26) ===
    # Identifier fields follow the SDK env-name convention from
    # docs_for_SDK/instructions/prototype_tracing.md (AGENT_ID, CLUSTER_ID,
    # POD_NAMESPACE, DISTRIBUTIVE). AEF_*-prefixed env vars are accepted as
    # a fallback so legacy deployment configs keep working.
    tracing_service_kafka_outbox_topic: str = Field(default="", validation_alias="TRACING_SERVICE_KAFKA_OUTBOX_TOPIC")
    tracing_max_payload_size: int = Field(default=10000, validation_alias="TRACING_MAX_PAYLOAD_SIZE")
    kafka_hosts: Annotated[list[str], NoDecode] = Field(
        alias="TRACING_SERVICE_KAFKA_BOOTSTRAP_SERVERS"
    )

    @field_validator("kafka_hosts", mode="before")
    @classmethod
    def parse_kafka_hosts(cls, v):
        if isinstance(v, str):
            return [h.strip() for h in v.strip("[] ").split(",") if h.strip()]
        return v

    aef_kafka_security_protocol: str = Field(
        default="PLAINTEXT", validation_alias="AEF_KAFKA_SECURITY_PROTOCOL"
    )
    aef_kafka_max_request_size: int = Field(
        default=10_485_760, validation_alias="AEF_KAFKA_MAX_REQUEST_SIZE"
    )
    aef_agent_id: str = Field(
        default="prototype-consultant",
        validation_alias=AliasChoices("AGENT_ID", "AEF_AGENT_ID"),
    )
    aef_cluster_id: str = Field(
        default="prototype-cluster-id1",
        validation_alias=AliasChoices("CLUSTER_ID", "AEF_CLUSTER_ID"),
    )
    aef_namespace: str = Field(
        default="prototype-namespace1",
        validation_alias=AliasChoices("POD_NAMESPACE", "AEF_NAMESPACE"),
    )
    aef_distributive: str = Field(
        default="prototype-distributive1",
        validation_alias=AliasChoices("DISTRIBUTIVE", "AEF_DISTRIBUTIVE"),
    )
    aef_langchain_callbacks_enabled: bool = Field(
        default=False,
        validation_alias="AEF_LANGCHAIN_CALLBACKS_ENABLED",
    )

    # Compiled regex — not config, not env-loadable. ClassVar tells pydantic
    # to skip it as a settings field; `settings.latex_pattern` still resolves.
    latex_pattern: ClassVar[re.Pattern] = re.compile(r'(\${1,2})(.+?)\1', re.DOTALL)

    @computed_field
    @property
    def adapters_api_base_url(self) -> str:
        """PALM.Security with the '/agent-tools' suffix appended once.

        Replaces the previous module-level constant. Existing imports of
        `ADAPTERS_API_BASE_URL` are kept via a module-level alias below.
        """
        return self.palm_security_api_url.rstrip("/") + "/agent-tools"


class GigaSettings(BaseSettings):
    """GigaChat client settings.

    All env-bound fields are namespaced under the `GIGACHAT_` prefix so that
    generic field names (`model`, `timeout`, `temperature`) cannot be
    overridden by accident through unrelated env vars in the deployment.

    Wrapper-fixed env vars: `GIGACHAT_API_URL`, `GIGACHAT_PREVIEW_MAIN_MODEL`,
    `GIGACHAT_PREVIEW_MODEL`, `GIGACHAT_PREVIEW_LIMITS_MODEL`,
    `GIGACHAT_PREVIEW_RATIO`, `GIGACHAT_CRT`, `GIGACHAT_KEY`.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="GIGACHAT_",
        case_sensitive=False,
        extra="ignore",
    )

    # GIGACHAT_API_URL — host only; `/v1` is appended by `base_url` below.
    api_url: str = "https://gigachat-ift.sberdevices.delta.sbrf.ru"

    is_local: bool = False
    profanity_check: bool = False
    verify_ssl_certs: bool = False

    # Generic-named tuning fields. They can still be overridden via
    # GIGACHAT_MODEL / GIGACHAT_TIMEOUT / ... but not via the unprefixed
    # MODEL / TIMEOUT envs.
    main_model: str = "GigaChat-2-Max"
    limits_model: str = "GigaChat-2-Max"
    main_model_timeout: int = 600
    main_model_temperature: float = 0.0
    model: str = "GigaChat-2"
    timeout: int = 300
    temperature: float = 0.0

    # Per-role max_tokens
    answer_max_tokens: int = 999999
    logic_max_tokens: int = 999999
    utility_max_tokens: int = 999999
    kpk_parse_max_tokens: int = 999999
    kpk_answer_max_tokens: int = 999999
    embed_batch_size: int = 25

    # SECURITY §26: PreView canary per LLM role
    preview_main_model: Optional[str] = None
    preview_model: Optional[str] = None
    preview_limits_model: Optional[str] = None
    preview_ratio: float = 0.0

    @field_validator("preview_ratio")
    @classmethod
    def _clamp_preview_ratio(cls, v: float) -> float:
        """SECURITY §26: clamp PreView canary into [0.0, 0.05].

        A bad env value should not block startup, but production canary traffic
        must stay within the 5% cap required before Main rollout.
        """
        if v < 0.0:
            return 0.0
        if v > MAX_PREVIEW_RATIO:
            return MAX_PREVIEW_RATIO
        return v

    @computed_field
    @property
    def base_url(self) -> str:
        return "{}/v1".format(self.api_url)

    @property
    def cert_file(self) -> Optional[str]:
        if self.is_local:
            return os.getenv("GIGACHAT_CRT", "gigachat/giga.pem")
        return None

    @property
    def key_file(self) -> Optional[str]:
        if self.is_local:
            return os.getenv("GIGACHAT_KEY", "gigachat/giga.key")
        return None


@lru_cache
def _get_settings() -> AppSettings:
    return AppSettings()


@lru_cache
def _get_giga_settings() -> GigaSettings:
    return GigaSettings()


settings: AppSettings = _get_settings()
giga_settings: GigaSettings = _get_giga_settings()

# Module-level aliases preserved for existing `from .config import X` callsites.
ADAPTERS_API_BASE_URL: str = settings.adapters_api_base_url
DB_APP_URL: str = settings.db_app_url

_LOGGER.info("--- ALL CONFIGS ARE READY! ---")
