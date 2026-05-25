# agent_consultant_app

Агент для консультирования клиентских менеджеров в UI `pss/agent-ui` по
вопросам методологии ценообразования на продукты срочных средств ЮЛ.

---

## 1. Суть проекта

Микросервис на FastAPI (порт **8081** по умолчанию, `APP_PORT`) поверх
LangGraph + GigaChat. Принимает вопросы от клиентских менеджеров и
маршрутизирует их по трём направлениям:

| Направление | Назначение |
| --- | --- |
| **RAG методологии** | Поиск ответа по markdown-документам в `md_docs/` (FAISS + GigaChatEmbeddings), с автокритикой и регенерацией при низкой уверенности. |
| **Tool (pricing / limits / both / deals_report)** | Расчёт показателей сделки через PSS-API (`agent-tools`): ЕТС, СРЛ, НОР, EVA, ставка безубыточности, влияние на лимит при котировании, отчёт по сделкам клиента за 30 дней. |
| **КПК** | Анализ изменений лимитов подразделений: режимы `single`, `all`, `find_deal`, `investigate_deal`, `client_history`, плюс XLSX-отчёт по «некорректным» сделкам. |

Кросс-турновое состояние (история диалога, последняя сделка, кэши)
хранит сам LangGraph через `MemorySaver`, привязанный к `thread_id`
(он же `chat_id`). Авторизация (`Redirect-Authorization`) пробрасывается
per-request через `RunnableConfig.configurable`, не сохраняется.

### Внешние интерфейсы

| Метод и путь | Назначение |
| --- | --- |
| `POST /chat` | Основной endpoint. Принимает `{message, chat_id}` + `Authorization` header. Возвращает `{answer, destination, confidence, sources, state, generated_report?}`. |
| `POST /api/kpk/incorrect-deals-report` | Скачивает XLSX-отчёт о некорректных сделках за дату, использует state-кэш `generated_reports`. |
| `GET /chat/{chat_id}/history` | Возвращает историю диалога из checkpointed state. |
| `POST /chat/{chat_id}/reset` | Очищает весь cross-turn-срез state: `messages`, `last_tool_payload`/`kind`/`selector`, `generated_reports`, `tool_cache`. |
| `GET /health`, `GET /ready` | Liveness / readiness — см. [Health-probes (SECURITY §19)](#health-probes-security-19). |
| `GET /settings` | Текущие значения порогов саммаризации и режима сериализации. |

### Внешние зависимости

- **GigaChat** — LLM для маршрутизации, RAG-генерации, критики,
  переформулирования вопросов, парсинга КПК-запросов и саммаризации
  истории. Шесть конфигов под разные роли в `llm_setup.py`.
- **PSS `agent-tools` API** — расчёт ставок, лимитов и отчётов.
  Базовый URL — `PALM_SECURITY_API_URL + '/agent-tools'`.
- **db_app** — асинхронный логгер обмена в `POST
  {DB_APP_URL}/v1/consultant-agent/logs` (best-effort, в фоне).
- **`md_docs/`** — markdown-источники методологии, индексируются в
  FAISS при старте процесса (`initialize_vector_db()`).

### Трейс операции (SECURITY §18 + §20)

Сервис — AI-агент (LLM + LangGraph), трейсинг реализован через **AEF Tracing SDK** (`sber-aef-tracing`) — proto-сообщения уходят в Kafka ФП AEF Controller, визуализируются в UI АС AEF Manager. Полный план миграции — [SECURITY_COMPLIANCE_SDK.md](../SECURITY_COMPLIANCE_SDK.md).

| ID | Жизненный цикл | Источник |
| --- | --- | --- |
| `trace_id` / `span_id` / `parent_span_id` | Один проход `/chat`. Иерархия спанов выстраивается nesting'ом контекстных менеджеров. | AEF SDK генерирует автоматически на каждый `aef_input_request`. |
| `session_id` | Стабильный за сессию диалога (= `chat_id`). | [api/app.py](api/app.py) — `session_id_cvar.set(request.chat_id)` перед созданием span'ов. |
| `message_id` | UNIQUE ключ идемпотентности отдельного Q/A; используется как PK `consultant_agent_log.id` и колонка `message_id` UNIQUE. | [api/app.py](api/app.py) — `uuid.uuid4()` на каждый `/chat`. |
| `chat_id` | Стабильный за сессию диалога, играет роль `thread_id` для LangGraph и `session_id` для `db_app` логов. Приходит от клиента в `POST /chat`. | [api/app.py](api/app.py) |
| `agent-id` / `cluster-id` / `namespace` / `distributive` | Статически на каждый span-batch. | Kafka-headers `AEFKafkaSender(headers={...})` из настроек [api/config.py](api/config.py). |

**Что эмитится в трейс:**

- **`input_request "chat"`** + вложенный **`agent_start`** — обёртка `/chat` в [api/app.py](api/app.py). На `agent_start` спане проставлены `aef.agent_uid`, `aef.ttl`, `aef.hops`, `aef.stop_event`, `aef.session_id` (§21).
- **`llm`** — ручные `aef_custom_span` вокруг общего LLM retry-wrapper в [api/graph_llm_wrappers.py](api/graph_llm_wrappers.py). LangChain `AEFHandler` callbacks выключены по умолчанию (`AEF_LANGCHAIN_CALLBACKS_ENABLED=false`), чтобы не ловить OpenTelemetry `Token was created in a different Context` на async callback end-событиях.
- **`output_request` / `api_call`** — ручные `aef_custom_span` на фоновый POST в `db_app/consultant-agent/logs` и на PSS `agent-tools` / КПК API в [api/app.py](api/app.py), [api/tools.py](api/tools.py) и [api/kpk_tools.py](api/kpk_tools.py).
- **`aef.is_mutation` / `aef.rollback_possible`** — на `aef_custom_span` обёртке фонового `db_app.consultant_log` POST (запись в `consultant_agent_log` — мутация без отката).

**StopEvent (§21).** При TTL `asyncio.wait_for(timeout=settings.graph_timeout_sec)` на `agent_start` проставляется `aef.stop_event="ttl_exceeded"`; при `phase="error"` от графа — `"phase_error"`.

**PreView GigaChat (§26).** `_pick(main, preview)` в [api/llm_setup.py](api/llm_setup.py) per-call выбирает Main или PreView; фактический LLM-вызов покрывается ручным `aef_custom_span` в общем retry-wrapper, а выбор инсталляции дополнительно логируется в structlog (`event=gigachat_installation_picked`).

Узлы графа дополнительно покрыты `_trace_node` ([api/tech_funcs.py](api/tech_funcs.py)) — на каждый узел пара `node_start` / `node_done` в stdout-логах. Это **дополнительный** structlog-аудит поверх SDK `chain`-спанов, не дубль; имена событий стабильны для существующих дашбордов FluentBit.

Cross-service trace propagation (`X-Run-Id` / `X-Thread-Id` headers) удалена — клиент (pss/agent-ui) и `db_app` не получают/не отправляют trace headers.

### Health-probes (SECURITY §19)

- **`GET /health`** — liveness. Всегда `200 {"status":"healthy", "timestamp":...}` пока процесс жив.
- **`GET /ready`** — readiness. `200 {"status":"ready", ...}` только после того как все стартовые проверки прошли; иначе `503 {"status":"not_ready", "timestamp":..., "failures":[{check,error,exc_type}, ...]}`.

Определения проверок и оркестрация — [api/startup_checkup.py](api/startup_checkup.py). Запуск — внутри `lifespan` ДО приёма трафика, sequential + fail-fast, per-check timeout (см. `readiness_*_timeout_sec` в [api/config.py](api/config.py)):

| check | что проверяет |
| --- | --- |
| `gigachat` | синтетический промпт через `utility_llm` (`"Ответь одним словом: OK"`), валидация непустого `content` |
| `rag` | три эталонных вопроса (NSO / стоимость фондирования / лимит КПК) через полный граф (`router → retrieve_documents → generate_rag → format_final_answer`), валидация непустого `final_answer` и непустых `sources` |
| `missing_data` | запрос `"Посчитай pricing"` без параметров через граф; валидация что граф не упал и вернул непустой ответ (уточнение) |

При провале — лог `startup_check_failed` с `check` / `error` / `exc_type`; сервер не падает (`/health=200`). Фоновая `recheck_loop` каждые `readiness_recheck_interval_sec` (по умолчанию 30s) переподнимает проверки и автоматически выставляет `ready=true` когда зависимость вернётся. Переходы логируются как `readiness_recheck_recovered` / `readiness_recheck_degraded`.

### Надёжность / Retry (SECURITY §22 + §23)

Внешние вызовы (LLM, PSS `agent-tools`, КПК API) обёрнуты в **tenacity** с экспоненциальным jitter-backoff и селективной политикой повторов. Параметры — в [api/config.py](api/config.py), LLM-обёртки — в [api/graph_llm_wrappers.py](api/graph_llm_wrappers.py).

| Слой | Где | Что ретраит | Параметры |
| --- | --- | --- | --- |
| **LLM** | `_invoke_structured_with_default` / `_invoke_text_with_default` / `_ainvoke_structured_with_default` / `_kpk_safe_text_invoke` ([api/graph_llm_wrappers.py](api/graph_llm_wrappers.py)) через `_llm_retrying_sync()` / `_llm_retrying_async()` | `429`, `5xx`, `httpx.TimeoutException` / `asyncio.TimeoutError`, transport (`ConnectError` / `RemoteProtocolError` / `OSError` / `requests.ConnectionError`). 4xx-non-429 и валидационные ошибки идут в node-level fallback сразу. | `llm_max_retries=3`, `llm_retry_base=0.5`, `llm_retry_max=5.0` |
| **HTTP (PSS)** | `execute_tool` / `execute_report_tool` ([api/tools.py](api/tools.py)) через `_http_retrying()` | `RequestException` (+ 5xx/429 через `raise_for_status()`); 4xx-non-429 → ERROR_TEXT без повторов | `http_max_retries=3`, `http_retry_base=0.5`, `http_retry_max=5.0` |
| **HTTP (КПК)** | `execute_kpk_limits_tool` / `download_incorrect_deals_report` ([api/kpk_tools.py](api/kpk_tools.py)) | то же самое | те же |

**Исключение:** `_kpk_safe_structured_invoke` намеренно без retry — там `ThreadPoolExecutor.future.result(timeout=120)` защищает от LLM-цикла на невалидном JSON. Ретрай умножил бы 120s × 3 и вышел бы за `graph_timeout_sec=600s`. В нём `_log_llm_exhausted` всё равно срабатывает в `except` для консистентности событий.

**Типизированные события при деградации GigaChat.** Классификатор `_classify_gigachat_error()` отображает любое исключение по `status_code` / `response.status_code` / типу:

| Класс ошибки | Событие | Ретраится? |
| --- | --- | --- |
| HTTP 429 | `gigachat_rate_limited` | да |
| HTTP 5xx | `gigachat_5xx_failed` | да |
| `httpx.TimeoutException` / `asyncio.TimeoutError` / `requests.Timeout` | `gigachat_timeout` | да |
| `httpx.ConnectError` / `RemoteProtocolError` / `OSError` / `requests.ConnectionError` | `gigachat_transport_error` | да |
| HTTP 4xx (не 429) | `gigachat_response_error` | нет |
| прочее | `gigachat_unknown_error` | нет |

На каждой попытке `before_sleep=_log_llm_retry` пишет событие с `attempt`, `next_wait_sec`, `exc_type`, `will_retry=True`. После исчерпания `_log_llm_exhausted()` пишет то же событие с `will_retry=False` перед существующим `audit.info("C4_FAIL_SERVICE_ACTION", ...)` и возвратом узлового fallback (default Pydantic / `default_text`). Глобальный safety net на уровне `/chat` хендлера — `ERROR_TEXT` ([api/config.py:22](api/config.py#L22)) — сохраняется как защита от несрабатывания узловых fallback'ов.

В Loki/OpenSearch: `event=gigachat_*` группируется → метрика «доля 429 vs 5xx vs timeout»; `will_retry=true/false` → доля исчерпаний. В UI AEF Manager Traces разбор цепочки ретраев по конкретному запросу — фильтр `session_id=<chat_id>` поднимает все спаны одного `/chat` вызова.

Подробнее — [SECURITY_COMPLIANCE_SDK.md](../SECURITY_COMPLIANCE_SDK.md).

---

## 2. Устройство графа

Состояние агента описано в `GraphState` (TypedDict, `graph_state.py`).
Ключевое поле — `messages: Annotated[List[BaseMessage], add_messages]`,
все остальные cross-turn поля имеют стандартный replace-reducer.

### Узлы и переходы

```text
                              ┌──────────┐
START ──────────────────────▶ │  router  │ ◀── route_query
                              └────┬─────┘
                                   │ route_branches
                ┌──────────────────┼─────────────────────────────┐
                ▼                  ▼                             ▼
       ┌────────────────┐  ┌──────────────┐              ┌─────────────────┐
       │ retrieve_docs  │  │ execute_tool │              │ kpk_preprocess  │
       └────────┬───────┘  └──────┬───────┘              └────────┬────────┘
                ▼                 │ after_execute_tool            ▼
       ┌────────────────┐         │                       ┌──────────────┐
       │  generate_rag  │     ┌───┴──────┬────────┐       │  kpk_parse   │
       └────┬───────────┘     ▼          ▼        ▼       └──────┬───────┘
            │ should_critique  retrieve  deals    final          │ _route_after_kpk_parse
            │                  _explain  _report                 │
       ┌────┴──────┐              │       │           ┌──────────┴──────────┐
       ▼           ▼              ▼       │           ▼                     ▼
   critique   format_final   generate     │     ┌──────────┐         ┌─────────────┐
       │      _answer        _explainer   │     │ kpk_tool │         │ kpk_finalize│
       │ after_critique          │        │     └────┬─────┘         └──────┬──────┘
       │                         ▼        │          ▼                       │
       ▼                  format_final    │   kpk_generate_answer            │
   refine_and             _answer         │          │                       │
   _reretrieve                │           │          ▼                       │
       │                      │           │     kpk_finalize ────────────────┘
       │ check_refine_result  │           │
       ▼                      │           │
   regenerate ─── format_final_answer     │
                              │           │
                              ▼           ▼
                         ┌────────────────────┐
                         │  append_response   │  ◀── adds AIMessage to messages
                         └──────────┬─────────┘
                                    ▼
                         ┌────────────────────┐
                         │ summarize_history  │  ◀── compacts old messages if N ≥ threshold
                         └──────────┬─────────┘
                                    ▼
                                   END
```

### Что делает каждый узел

| Узел | Файл | Назначение |
| --- | --- | --- |
| `router` | `graph_nodes_rag.route_query` | LLM-классификация запроса в `rag_methodology` / `unsupported_calculation` / `kpk_limits`. |
| `execute_tool` | `graph_nodes_tool.execute_tool_node` | Резолвинг контекста сделки (`resolve_context`), вызов PSS-API с кэшированием через `tool_cache`. Пишет `last_tool_payload`/`kind`/`selector` в state. |
| `deals_report` | `graph_nodes_tool.deals_report` | Отчёт по сделкам клиента за 30 дней по списку ИНН. |
| `retrieve_explain` / `generate_explainer` | `graph_nodes_tool` | После tool-вызова — методологическое объяснение в текстовой форме поверх численного результата. |
| `retrieve_documents` / `generate_rag` | `graph_nodes_rag` | Базовый RAG: контекстуализация вопроса (`rag_context_chain`), FAISS-поиск, re-rank через LLM, генерация ответа со скором уверенности. |
| `critique_rag` | `graph_nodes_rag.critique_answer` | LLM-критика ответа при низком confidence. |
| `refine_and_reretrieve` / `regenerate_answer` | `graph_nodes_rag` | Переформулирование вопроса и повторная генерация. |
| `format_final_answer` | `graph_nodes_rag` | Финальная нормализация (LaTeX-санитайзинг, группировка больших чисел). |
| `kpk_preprocess` / `kpk_parse` / `kpk_tool` / `kpk_generate_answer` / `kpk_finalize` | `graph_nodes_kpk` | КПК-пайплайн: разбор намерения, выбор режима, вызов tool, генерация ответа. |
| `append_response` | `graph_nodes_messages.append_response_node` | Эмитит `{"messages": [AIMessage(final_answer)]}` — reducer `add_messages` дописывает к state. |
| `summarize_history` | `graph_nodes_messages.summarize_history_node` | При `len(messages) ≥ summarization_treshold` — `RemoveMessage`-тумб­стоуны на старые + одна `AIMessage` со сводкой. |

### Маршрутизация

Условные рёбра (все они — pure-функции в `graph_builder.py`):

- **`route_branches`** — после router:
  - `dest == "kpk_limits"` → `kpk_preprocess`;
  - `dest == "unsupported_calculation"` → `execute_tool`;
  - `dest == "rag_methodology"` + есть контекст (`last_tool_payload`
    или `last_selector` в state) + в тексте calc-trigger или referal
    («посчитай», «какая ставка», «по этой сделке») → **override**
    на `execute_tool`;
  - иначе → `retrieve_documents`.
- **`after_execute_tool`** — `deals_report` / `explain` / `final`.
- **`should_critique`** — пропускаем критику при `regen_attempts ≥
  max_retries` или `confidence_score > threshold`.
- **`after_critique`** — `end_critique` / `refine`.
- **`check_refine_result`** — `skip_regen` / `do_regen`.
- **`_route_after_kpk_parse`** — `response_state == "input-required"`
  → сразу к `kpk_finalize`, иначе к `kpk_tool`.

### Что хранит state между ходами

| Поле | Что в нём | Кто пишет | Когда читается |
| --- | --- | --- | --- |
| `messages` | Полная история диалога (`HumanMessage`/`AIMessage`/summary) | seed-input в `ainvoke` + `append_response_node` + `summarize_history_node` (через `RemoveMessage`) | каждый узел, использующий `chat_history` в промпте |
| `last_tool_payload` | Сырой результат последнего успешного tool-вызова (components, inputs_used, explain_map) | `execute_tool_node` через return | `resolve_context`, `_deal_id_from_state`, `prefetched_row_from_payload` |
| `last_tool_kind` | `"pricing"` / `"limits"` / `"both"` | `execute_tool_node` | для контекста ribbon и переключения форматирования |
| `last_selector` | Нормализованный селектор сделки (без `inn`) | `execute_tool_node` | `resolve_context` ([CONTINUE/NEW] решение), `route_branches` (has_ctx) |
| `generated_reports` | `{f"incorrect_deals_report:{report_dt}": result}` | `_ensure_incorrect_deals_report_cached` | тот же endpoint при повторном вызове |
| `tool_cache` | `{_key_for_cache(...): result}` для PSS API | `execute_tool_node` (через return дополняет `cache_out`) | следующий `execute_tool` в этой же сессии |

### Что НЕ в state (per-request runtime)

- `thread_id` → `configurable["thread_id"]` (равен `chat_id`).
- `auth_header` → `configurable["auth_header"]`. Узлы читают через
  второй параметр сигнатуры: `async def node(state, config=None)`.

---

## 3. Особенности реализации (что поменяли, зачем, что важно при доработках)

Раньше всё кросс-турновое состояние жило в отдельном объекте
`InMemoryChatStore`, который таскался через state как `_store: Any` и
`_chat_id: str`. Это блокировало миграцию на persistent checkpointer и
требовало двух источников правды (`store` для записи, state для
прогона графа).

### Что поменяли (последовательность шагов)

1. **Подключили `MemorySaver`** в `graph.py`, граф компилируется через
   `build_graph(checkpointer=checkpointer)`, при каждом `ainvoke`
   прокидывается `config={"configurable": {"thread_id": chat_id}}`.
2. **`last_tool_payload` / `last_tool_kind` / `last_selector`** —
   перенесены из store в state. `resolve_context(text, sel, state)`,
   `_deal_id_from_state(state)`, `route_branches(state)` читают state
   напрямую; `prefetched_row_from_payload(payload)` — pure-функция.
3. **История диалога** — `chat_history` ушёл, на его место встал
   `messages: Annotated[List[BaseMessage], add_messages]`. Узлы читают
   `state.get("messages", [])`, ключ `"chat_history"` в `chain.ainvoke`
   остался (это переменная промпта). Финальный assistant-ответ
   записывает узел `append_response_node`, компактирует — узел
   `summarize_history_node` через `RemoveMessage`.
4. **`generated_reports`** (кэш XLSX-отчётов) — перенесён в state,
   `_ensure_incorrect_deals_report_cached` работает через
   `aget_state`/`aupdate_state` (с `as_node=START`).
5. **`tool_cache`** — перенесён в state. `tools.execute_tool` теперь
   stateless относительно store: принимает `tool_cache: dict | None`,
   возвращает `(res, from_cache, new_cache)`. Узел кладёт `new_cache`
   в return.
6. **`auth_header`** — больше не хранится. Endpoint берёт его из HTTP
   header и кладёт в `configurable["auth_header"]`. Узлы и tool-функции
   читают через config-параметр и передают в `_auth_headers(auth_header)`.
7. **`InMemoryChatStore` удалён** вместе с `storage_cleanup_task`,
   `global_store`, `set_auth_header`-логикой и всей TTL-чисткой
   (с `MemorySaver` restart процесса = очистка по дизайну).

### Зачем

- **Готовность к persistent checkpointer.** Когда понадобится
  переключить `MemorySaver` на `AsyncPostgresSaver` (есть готовый
  паттерн в `agent_app/app/core/checkpointer.py`) — никаких изменений
  в узлах не требуется, всё сериализуемо.
- **Единый источник правды.** Любое cross-turn-изменение проходит
  через return узла → reducer → state. Двойных записей нет, дрейф
  между store и state невозможен.
- **Резко упрощённый `app.py`.** Нет ручной клейки истории, нет
  отдельной `_get_history_from_checkpointer`, нет `aupdate_state`
  снаружи графа для каждого ответа — всё делает сам граф.

### Что важно учесть при доработках

**Узлы.**

- Любое cross-turn-значение возвращайте через return-dict узла:
  `return {"my_new_field": value}`. Reducer применит его к state по
  правилу поля (replace по умолчанию, либо `add_messages` для
  `messages`).
- Если узлу нужен `chat_id` или `auth_header` — добавьте параметр
  `config=None` в сигнатуру:

  ```python
  async def my_node(state, config=None):
      cfg = (config or {}).get("configurable") or {}
      chat_id = cfg.get("thread_id") or "default"
      auth = cfg.get("auth_header")
  ```

  LangGraph сам подаст runnable-config через рефлексию.

**State.**

- Все новые cross-turn поля объявляйте в `GraphState`. Для просто
  «последнее значение» — обычный тип, replace-семантика. Для
  списков-логов — `Annotated[List[...], add_messages]` или собственный
  reducer.
- При прямой записи через `compiled_graph.aupdate_state(config, …)`
  **обязательно** передавайте `as_node=START` — иначе на свежем
  thread'е (где ни одного узла ещё не отработало) LangGraph падает
  с `InvalidUpdateError: Ambiguous update, specify as_node`.
  Тестово отловлено в [test_cache.py](../tests/agent_consultant_app/test_cache.py),
  оба production-вызова в `app.py` уже починены.
- Для очистки `messages` извне графа используется
  `RemoveMessage(id=REMOVE_ALL_MESSAGES)` (из
  `langgraph.graph.message`). Для точечного удаления —
  `RemoveMessage(id=msg.id)` для каждого id.

**Кэши.**

- `tool_cache`: key = `_key_for_cache(tool, user_text, explicit, prefetched_row)`
  (`tools._key_for_cache`). Кэшируются **только** успешные ответы
  PSS API. Ошибки в кэш не попадают.
- `generated_reports`: key = `f"incorrect_deals_report:{report_dt}"`.
  Хитом считается только entry с `status=="success"` и непустым
  `content` (`_is_valid_cached_report`). Прочее принудительно
  re-download.
- Оба кэша per-thread; cross-thread изоляция гарантирована
  checkpointer'ом.

**Auth.**

- НЕ кладите `auth_header` в state — он request-scoped. Передавайте
  через `configurable`. Если добавляете новую функцию, делающую HTTP-
  запросы к PSS — принимайте `auth_header: str | None` параметром и
  собирайте dict через `_auth_headers(auth_header)` (есть в
  `tools.py` и `kpk_tools.py`).

**Endpoint `/reset`.**

- Если добавляется новое cross-turn-поле в `GraphState`, добавьте
  его в payload `aupdate_state` в `reset_chat`, иначе `/reset` будет
  оставлять «хвост».

**Саммаризация.**

- Параметры (`summarization_treshold`, `summarization_window`) — в
  `AppSettings`. Pure-функция `summarize_history_if_needed` в
  `summarizer.py` сохранена для unit-тестов с replace-семантикой.
  В графе работает `summarize_history_node`, который использует
  `RemoveMessage`-стиль (add_messages-совместимый).

**Sys.path / импорты.**

- Модули `api/*.py` используют двойной try-import (`from X` /
  `from .X`). Это поддерживает запуск и как пакет
  (`agent_consultant_app.api.X`), и как top-level (если `api/` в
  `sys.path`). В тестах используется первая форма; конфликт с
  namespace-package `config` от соседних сервисов решается
  алиасом в [conftest.py](../tests/agent_consultant_app/conftest.py).

---

## 4. TODO

### TTL / retention при переходе на `AsyncPostgresSaver`

Сейчас checkpointer — `MemorySaver`, и весь cross-turn state живёт
in-process. Рестарт сервиса = полная очистка, поэтому никакой
TTL-логики не нужно. Это **временно**.

До Шага 2.5 в `app.py` крутилась фоновая задача `storage_cleanup_task`
(каждые 10 минут вычищала из `InMemoryChatStore` сессии без активности
дольше 2 часов: `SESSION_TTL_SEC = 3600 * 2`, `CLEANUP_INTERVAL_SEC =
600`). Эту задачу удалили вместе со store, потому что у MemorySaver
нет понятия «expired thread».

Когда checkpointer заменяется на `AsyncPostgresSaver` (см. готовый
паттерн в [agent_app/app/core/checkpointer.py](../agent_app/app/core/checkpointer.py)),
threads начнут жить в БД **бесконечно** — БД сама не чистится. Нужно
реализовать retention:

- **Что чистить:** строки в служебных таблицах LangGraph
  (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`) по
  `thread_id`, у которого последний `created_at`/`updated_at` старше
  TTL. Реальные имена колонок и таблиц проверить по миграциям
  `AsyncPostgresSaver.setup()` на актуальной версии `langgraph-
  checkpoint-postgres`.
- **TTL по умолчанию:** оставить 2 часа (как было в
  `storage_cleanup_task`), вынести в `AppSettings` отдельным
  параметром, чтобы можно было крутить из env.
- **Как чистить:** два варианта, выбрать при имплементации:
  1. Фоновая корутина в `app.py`-lifespan, аналог старой
     `storage_cleanup_task`, но через SQL `DELETE` по тем же
     condition'ам.
  2. PostgreSQL-сторона (`pg_cron` job или внешний крон, дёргающий
     `psql -c "DELETE FROM …"`) — снимает нагрузку с приложения и
     переживает рестарты сервиса.
- **Что не забыть:**
  - `/chat/{id}/reset` уже корректно сбрасывает state через
    `aupdate_state` с `as_node=START` — после очистки thread должен
    остаться рабочим (новые `ainvoke` создадут новый checkpoint).
  - Retention должен учитывать «активные» thread'ы — last activity
    timestamp = max(`created_at`) среди checkpoints этого `thread_id`,
    а не время первого создания.
  - В тестах добавить характеризационный кейс «после retention
    `aget_state` возвращает пустой snapshot, следующий `ainvoke`
    стартует с чистого листа».

---

## Запуск

```bash
# из корня monorepo
.venv/bin/python -m agent_consultant_app.main
# либо напрямую uvicorn:
.venv/bin/uvicorn agent_consultant_app.api.app:app --host 0.0.0.0 --port 8081 --reload
```

Минимально нужные env-переменные (см. `.env`):

| Переменная | Назначение |
| --- | --- |
| `PALM_SECURITY_API_URL` | Базовый URL PSS, к нему дописывается `/agent-tools`. |
| `GIGACHAT_API_URL`, `GIGACHAT_CRT`, `GIGACHAT_KEY` | Подключение к GigaChat (для локального режима). |
| `DB_APP_URL` | Адрес db_app для best-effort логирования обменов. |
| `APP_HOST`, `APP_PORT` | Привязка uvicorn, по умолчанию `0.0.0.0:8081`. |
| `STRICT_SERIALIZATION` | Legacy-флаг режима саммаризации (см. `/settings`). |

## Тесты

См. [tests/agent_consultant_app/](../tests/agent_consultant_app/).
