# db_app

Внутренний HTTP-сервис — единая точка доступа к БД для `mail_app`, `agent_treasurer_app` и `agent_tools_app`.

```text
mail_app  ──HTTP──▶  db_app  ──▶  PostgreSQL
```

> **`agent_treasurer_app`** работает по схеме `{id, message} → graph → {id, answer}` и не обращается к `db_app`. Состояние LangGraph тредов хранится отдельно (сейчас — `MemorySaver`, в планах — собственная PostgreSQL-база агента).

---

## Быстрый старт

```bash
cd db_app
pip install -r requirements.txt

# Запустить сервис (БД-подключение через TOML-файл, путь в DB_SECRETS)
DB_SECRETS=/path/to/db_secrets.toml \
  uvicorn db_app.main:app --port 444 --reload
```

Пример `db_secrets.toml`:

```toml
[connections.db1]
dialect = "postgresql+asyncpg"
username = "user"
password = "pass"
host = "localhost"
port = "5432"
database = "mail_server"
```

Или с готовым URL:

```toml
[connections.db1]
url = "postgresql+asyncpg://user:pass@localhost:5432/mail_server"
```

Swagger UI: `http://localhost:444/docs`

---

## Конфигурация

### Подключение к БД

Подключение настраивается через TOML-файл, путь к которому указывается в переменной `DB_SECRETS`. Формат файла совместим с платформой деплоя.

### Переменные окружения

Все переменные с префиксом `DB_`:

| Переменная     | Обязательная | По умолчанию | Описание |
|----------------|:---:|---|---|
| `DB_SECRETS`   | да  | — | Путь к TOML-файлу с настройками подключения к БД |
| `DB_PORT`      | нет | `444` | Порт сервиса |
| `DB_LOG_LEVEL` | нет | `INFO` | Уровень логирования |

---

## Миграции

Миграции выполняются платформой деплоя через **Liquibase**. SQL-файлы хранятся в `migration/db1/changelog.sql`.

Формат changeset:

```sql
--liquibase formatted sql

--changeset author:description
CREATE TABLE ...;
--rollback DROP TABLE ...;
```

Конфигурация миграций описана в `description.yaml` → `app.connections.databases`.

---

## Модель данных

База `mail_server`.

```
threads
messages
outbox_tasks
approve_deal_snapshots
approve_tasks
deal_parsing_log
deal_snapshots
```

### `threads`

| Колонка | Тип | |
|---|---|---|
| `thread_id` | VARCHAR PK | корневой RFC `Message-ID` первого письма в треде |
| `subject` | VARCHAR nullable | |
| `created_at` | TIMESTAMPTZ | default now() |

`thread_id` вычисляется на стороне `mail_app` при получении письма: первый `Message-ID` из заголовка `References`, либо `In-Reply-To`, либо собственный `Message-ID` (если это первое письмо треда). `db_app` не генерирует его — только принимает и хранит через `get_or_create`.

### `messages`

| Колонка | Тип | |
|---|---|---|
| `message_id` | VARCHAR PK | RFC `Message-ID`, генерируется `mail_app` |
| `thread_id` | FK → `threads.thread_id` | |
| `author_email` | VARCHAR | |
| `body_text` | TEXT nullable | |
| `body_html` | TEXT nullable | |
| `headers_json` | JSONB nullable | все заголовки письма |
| `created_at` | TIMESTAMPTZ | |
| `run_id` | VARCHAR(64) nullable | UID операции для §18+§20 (см. [Трейс операции](#трейс-операции-security-18--20)); индексируется (`ix_messages_run_id`) |

`message_id` — одновременно PK и ключ идемпотентности. `db_app` не генерирует ID — только принимает и хранит.

### `outbox_tasks`

| Колонка | Тип | |
|---|---|---|
| `id` | VARCHAR PK | |
| `task_type` | VARCHAR | `PROCESS_INCOMING`, `SEND_SMTP`, ... |
| `email_id` | FK → `messages.message_id` nullable | |
| `payload_json` | JSONB nullable | произвольный payload для воркера |
| `task_key` | VARCHAR UNIQUE | ключ идемпотентности |
| `status` | VARCHAR | `NEW` → `PROCESSING` → `DONE` / `RETRYING` / `FAILED` |
| `attempt` | INT | инкрементируется при каждом dequeue |
| `next_retry_at` | TIMESTAMPTZ nullable | когда задача снова готова к обработке |
| `claim_token` | VARCHAR nullable | токен текущего воркера |
| `claim_until` | TIMESTAMPTZ nullable | дедлайн claim-а |
| `created_at` / `updated_at` | TIMESTAMPTZ | |
| `run_id` | VARCHAR(64) nullable | UID операции (см. [Трейс операции](#трейс-операции-security-18--20)); индексируется (`ix_outbox_tasks_run_id`). В `payload_json` SMTP-воркер дублирует значение, чтобы при `dequeue` восстановить `run_id` в contextvars. |

### `approve_tasks`

Задача на одобрение/проверку сделки. **Родительская** для `approve_deal_snapshots`: сначала создаётся задача, затем к ней привязывается snapshot. Связь 1:1 (UNIQUE FK на стороне snapshot'а).

| Колонка | Тип | |
|---|---|---|
| `task_id` | VARCHAR PK | внутренний UUID задачи (стабилен между попытками retry) |
| `calculation_id` | VARCHAR UNIQUE | внешний id расчёта из CSP, ключ дедупликации |
| `task_dttm` | TIMESTAMPTZ | default now(), время создания |
| `updated_at` | TIMESTAMPTZ | default now(), время последнего изменения; обновляется приложением в `update_status` |
| `task_status` | VARCHAR | `RECEIVED` → `ENRICHED` → `SENT_TO_AGENT` → `COMPLETED` / `REJECTED`, либо `FAILED` |
| `attempt_count` | INT | default 0, счётчик переотправок агенту (нужен TTL watchdog'у) |
| `agent_answer` | TEXT nullable | текстовый `reason` от агента |
| `deal_status` | VARCHAR nullable | финальное решение: `COMPLETED` / `REJECTED` / null |
| `error_message` | TEXT nullable | сообщение об ошибке при `FAILED` |
| `run_id` | VARCHAR(64) nullable | UID операции (см. [Трейс операции](#трейс-операции-security-18--20)); индексируется (`ix_at_run_id` partial WHERE NOT NULL). Перезаписывается на свежий при FAILED-reprocessing — каждая новая попытка СО стороны источника = новая логическая операция, retry внутри одной попытки идут под тем же `run_id`. |

### `approve_deal_snapshots`

Snapshot одного расчёта ставки с полным набором параметров от `agent_tools_app` и `cfc-service`. **Дочерняя** к `approve_tasks` — без задачи существовать не может.

| Колонка | Тип | |
|---|---|---|
| `calc_id` | VARCHAR PK | id расчёта во внешней системе (CalcFundCost) |
| `task_id` | VARCHAR UNIQUE FK → `approve_tasks.task_id` ON DELETE CASCADE | привязка к задаче 1:1 |
| `calc_dttm` | TIMESTAMPTZ | default now() |
| `policy_rate` | NUMERIC nullable | итоговая ставка калькулятора |
| `ets` | NUMERIC nullable | чистая ЕТС в базисе расчёта |
| `for_rate` | NUMERIC nullable | ставка ФОР |
| `eva_target` | NUMERIC nullable | целевая EVA |
| `eva_indicative` | NUMERIC nullable | индикативная EVA |
| `asv` | NUMERIC nullable | премия АСВ |
| `option_price` | NUMERIC nullable | стоимость опциона |
| `crl` | NUMERIC nullable | Cost of Liquidity Risk |
| `cfc_so` | NUMERIC nullable | CFC: ставка So |
| `cfc_ets` | NUMERIC nullable | CFC: ЕТС |
| `cfc_for_rate` | NUMERIC nullable | CFC: ставка ФОР |
| `cfc_eva_target` | NUMERIC nullable | CFC: целевая EVA |
| `cfc_eva_indicative` | NUMERIC nullable | CFC: индикативная EVA |
| `cfc_asv` | NUMERIC nullable | CFC: ставка АСВ |
| `cfc_option_price` | NUMERIC nullable | CFC: стоимость опциона |
| `cfc_k_liq_fund` | NUMERIC nullable | CFC: коэффициент ликвидности |
| `run_id` | VARCHAR(64) nullable | UID операции, под которым был создан snapshot (см. [Трейс операции](#трейс-операции-security-18--20)); индексируется (`ix_ads_run_id` partial WHERE NOT NULL). |

### `deal_parsing_log`

Один фрагмент письма, отправленный на парсинг.

| Колонка | Тип | |
|---|---|---|
| `id` | VARCHAR PK | uuid, генерируется при записи |
| `message_id` | FK → `messages.message_id` | |
| `fragment_idx` | INT | индекс фрагмента (0, 1, 2…) |
| `fragment_text` | TEXT | исходный текст фрагмента |
| `extraction_status` | VARCHAR | `success` / `failed` / `empty` (default) |
| `intent` | VARCHAR nullable | `new` / `negotiate` / `approve` / `provide_data` / `escalate` |
| `deal_number` | INT nullable | номер сделки, к которой отнесён фрагмент |
| `parsed_conditions` | JSONB nullable | `{product, inn, term_days, volume, ...}` |
| `error_details` | TEXT nullable | текст ошибки при `failed` |
| `warnings` | JSONB nullable | предупреждения валидации |
| `created_at` | TIMESTAMPTZ | default now() |
| `run_id` | VARCHAR(64) nullable | UID операции (см. [Трейс операции](#трейс-операции-security-18--20)); индексируется (`ix_dpl_run_id` partial WHERE NOT NULL). |

### `deal_snapshots`

Состояние сделки после одного раунда переговоров. Каждый раунд — новая строка, история накапливается.

| Колонка | Тип | |
|---|---|---|
| `id` | VARCHAR PK | uuid |
| `thread_id` | FK → `threads.thread_id` | |
| `deal_number` | INT | порядковый номер сделки в треде |
| `iteration` | INT | номер раунда переговоров |
| `incoming_conditions` | JSONB nullable | условия, запрошенные клиентом |
| `request_rate` | NUMERIC nullable | ставка, запрошенная клиентом |
| `limit_rate` | NUMERIC | лимитная ставка |
| `avg_hist_rate` | NUMERIC nullable | средняя историческая ставка |
| `model_rate` | NUMERIC nullable | ставка от модели |
| `policy_rate` | NUMERIC | ставка от калькулятора |
| `final_rate` | NUMERIC | итоговая предложенная ставка |
| `proposed_conditions` | JSONB nullable | финальное предложение агента |
| `status` | VARCHAR | `NEGOTIATING` / `ACCEPTED` / `ESCALATED` |
| `escalation_reason` | TEXT nullable | причина эскалации |
| `parsing_log_id` | FK → `deal_parsing_log.id` | |
| `created_at` | TIMESTAMPTZ | default now() |
| `utilizes_limit` | BOOL | использовался ли лимит контрагента |
| `agent_text` | TEXT | текст ответа агента |
| `run_id` | VARCHAR(64) nullable | UID операции, в рамках которой записан snapshot (см. [Трейс операции](#трейс-операции-security-18--20)); индексируется (`ix_ds_run_id` partial WHERE NOT NULL). |

### `consultant_agent_logs`

Лог взаимодействий с агентом-консультантом СЮЛ. Standalone-таблица: внешних ключей к остальным сущностям нет, потому что агент-консультант живёт вне этой репы и не делит с ней `thread_id` / `message_id`.

| Колонка | Тип | |
|---|---|---|
| `id` | VARCHAR PK | id записи лога (генерируется вызывающей стороной) |
| `question_dttm` | TIMESTAMPTZ | время вопроса, default now() |
| `session_id` | VARCHAR | id чат-сессии |
| `message_id` | VARCHAR UNIQUE | id сообщения внутри агента; ключ идемпотентности |
| `user_id` | VARCHAR | id пользователя |
| `question` | TEXT | текст вопроса |
| `agent_branch` | VARCHAR nullable | ветка агента: `RAG` / `pricing` / `limits` / `report` / `limits_kpk` / … |
| `agent_answer` | TEXT nullable | ответ агента (заполняется сразу или patch-ом позже) |
| `answer_dttm` | TIMESTAMPTZ nullable | время ответа |
| `source_system` | VARCHAR | откуда пришёл вопрос: `support` (default) / `km_from_fox` / … |

---

## Трейс операции (SECURITY §18 + §20)

Сервис — HTTP-шлюз к Postgres (не AI-агент), поэтому из требований SECURITY §18+§20 реализован только транспортный слой: приём идентификаторов на входе и персистенция их вместе с прикладными данными. **Никаких `tool_call`/`llm_call` обёрток нет.**

### Приём идентификаторов

HTTP-мидлвара [main.py:30-46](main.py#L30) (`trace_context_middleware`) читает из входящих запросов `X-Run-Id` и `X-Thread-Id`, биндит в `structlog.contextvars`, чистит на выходе. `merge_contextvars` подключён в [app/logging_config.py](app/logging_config.py) — все события `db_app` под одним запросом автоматически получают `run_id`/`thread_id` в JSON-логе.

### Персистенция

Колонки `run_id VARCHAR(64) NULL` добавлены в таблицы, описывающие прикладные операции:

| Таблица | Индекс |
| --- | --- |
| `messages` | `ix_messages_run_id` (полный) |
| `outbox_tasks` | `ix_outbox_tasks_run_id` (полный) |
| `approve_tasks` | `ix_at_run_id` (partial WHERE NOT NULL) |
| `approve_deal_snapshots` | `ix_ads_run_id` (partial WHERE NOT NULL) |
| `deal_parsing_log` | `ix_dpl_run_id` (partial WHERE NOT NULL) |
| `deal_snapshots` | `ix_ds_run_id` (partial WHERE NOT NULL) |

Колонка не FK, не UNIQUE — один `run_id` может встречаться в нескольких строках (одна логическая операция порождает запись в `messages`, потом несколько в `deal_parsing_log`/`deal_snapshots`, и т.п.). Семантика: фильтр `WHERE run_id = '<X>'` поднимает все артефакты одной операции по всему сервису одним запросом.

Для `consultant_agent_logs` отдельной колонки `run_id` нет — там `id` (PK) и есть `run_id` по соглашению вызывающего сервиса (в `agent_consultant_app` они совпадают). Эндпоинт `/v1/consultant-agent/logs/{run_id}` (и его `/answer` PATCH) использует это имя в path-параметре.

---

## API

### Messages

#### `POST /v1/messages/ingest`
Сохранить входящее письмо и создать задачу `PROCESS_INCOMING`. Операция атомарна.

```json
{
  "message_id": "msg-001@example.com",
  "author_email": "client@example.com",
  "sender_name": "Иванов Иван",
  "reply_to_email": "deals-desk@example.com",
  "subject": "Re: предложение",
  "body_text": "...",
  "headers_json": {"In-Reply-To": "...", "References": "..."},
  "thread_id": "thread-001@example.com",
  "task_key": "ingest:msg-001@example.com"
}
```

`reply_to_email` опционально и берётся из RFC-2822 заголовка `Reply-To` входящего письма. Если задано, `mail_app` отправит ответ на этот адрес вместо `author_email`. Поле прокидывается в `payload_json` задачи `PROCESS_INCOMING`.

#### `POST /v1/messages/outgoing`
Сохранить исходящее письмо и создать задачу `SEND_SMTP`. Операция атомарна.

#### `GET /v1/messages/by-author/{author_email}?limit=50&offset=0`
Письма автора, отсортированные по `created_at`.

#### `GET /v1/messages/threads/{thread_id}/messages`
Все письма в треде, отсортированные по `created_at`.

---

### Outbox

#### `POST /v1/outbox/dequeue`
Claim-нуть батч готовых задач (`FOR UPDATE SKIP LOCKED`). Возвращает задачи с `claim_token`.

```json
{ "task_type": "PROCESS_INCOMING", "limit": 10, "claim_ttl_seconds": 300 }
```

`task_type` опционален — если `null`, вернёт задачи любого типа.

Воркер должен завершить обработку до `claim_until`. Если нет — задача будет переотдана другому воркеру при следующем dequeue.

#### `PATCH /v1/outbox/{id}/status`
Обновить статус задачи. Требует `claim_token` из dequeue. Возвращает `409` при несовпадении токена.

```json
{
  "status": "DONE",
  "claim_token": "...",
  "error": null,
  "next_retry_at": null
}
```

Допустимые переходы: `PROCESSING → DONE | RETRYING | FAILED`.

---

### Approve

#### `POST /v1/approve/tasks`

Создать задачу на одобрение. Идемпотентно по `task_id` (`INSERT ... ON CONFLICT DO NOTHING`). UNIQUE-индекс на `calculation_id` страхует от дублей при гонках.

```json
{
  "task_id": "uuid-задачи",
  "calculation_id": "ext-csp-id",
  "task_status": "RECEIVED",
  "attempt_count": 0
}
```

#### `GET /v1/approve/tasks/{task_id}`

Получить задачу по внутреннему UUID. Возвращает 404 если нет.

#### `GET /v1/approve/tasks/by-calculation/{calculation_id}`

Получить задачу по внешнему `calculation_id` — основной путь дедупликации со стороны `approve-service`. Возвращает 404 если нет.

#### `GET /v1/approve/tasks?task_status=...&calculation_id=...&older_than_minutes=...`

Список задач с опциональными фильтрами, отсортированных по `task_dttm` ASC.

`older_than_minutes` — вернуть только задачи, у которых `updated_at` старше N минут. Используется TTL watchdog'ом `approve-service` для поиска зависших задач.

#### `PATCH /v1/approve/tasks/{task_id}/status`

Обновить статус, счётчик попыток, ответ агента и сообщение об ошибке. Поля кроме `task_status` опциональны. `updated_at` обновляется автоматически при каждом вызове.

```json
{
  "task_status": "COMPLETED",
  "deal_status": "COMPLETED",
  "agent_answer": "Сделка одобрена, ставка в пределах лимита.",
  "attempt_count": 1,
  "error_message": null
}
```

#### `POST /v1/approve/snapshots`

Создать snapshot расчёта ставки. Требует существующего `task_id` (FK).

```json
{
  "calc_id": "id-сделки-внешний",
  "task_id": "uuid-задачи",
  "policy_rate": 12.5,
  "ets": 8.1,
  "cfc_so": 12.3,
  "..."
}
```

#### `GET /v1/approve/snapshots/by-task/{task_id}`

Получить snapshot, привязанный к задаче (1:1 через UNIQUE FK). Используется TTL watchdog'ом для retry: при переотправке агенту параметры тянутся отсюда. Возвращает 404 если нет.

---

### Deals

#### `POST /v1/deals/parsing-log`
Создать запись парсинга фрагмента письма.

```json
{
  "message_id": "msg-001@example.com",
  "fragment_idx": 0,
  "fragment_text": "ИНН 1234567890, депозит 100 млн на 90 дней...",
  "extraction_status": "success",
  "intent": "new",
  "deal_number": 1,
  "parsed_conditions": {"product": "deposit", "inn": "1234567890", "term_days": 90}
}
```

#### `POST /v1/deals/snapshots`
Создать snapshot состояния сделки после раунда переговоров.

```json
{
  "thread_id": "thread-001@example.com",
  "deal_number": 1,
  "iteration": 1,
  "incoming_conditions": {"product": "deposit", "inn": "1234567890", "term_days": 90},
  "request_rate": 15.0,
  "limit_rate": 15.0,
  "avg_hist_rate": 13.5,
  "model_rate": 14.2,
  "policy_rate": 14.5,
  "final_rate": 14.5,
  "proposed_conditions": {"rate": 14.5, "term_days": 90, "volume": 100000000},
  "status": "NEGOTIATING",
  "parsing_log_id": "uuid-записи-парсинга",
  "utilizes_limit": true,
  "agent_text": "Предлагаем ставку 14.5% годовых на 90 дней."
}
```

---

### Consultant Agent Logs

Лог агента-консультанта СЮЛ. Standalone — никаких FK к `messages` / `threads`.

#### `POST /v1/consultant-agent/logs`
Создать запись Q/A. Идемпотентно по `message_id`.

```json
{
  "id": "log-001",
  "session_id": "sess-001",
  "message_id": "msg-001",
  "user_id": "user-42",
  "question": "Какой лимит на контрагента ИНН ...?",
  "agent_branch": "limits",
  "agent_answer": null,
  "source_system": "support"
}
```

`agent_answer` / `answer_dttm` опциональны при создании — могут быть заполнены позже через PATCH.

#### `GET /v1/consultant-agent/logs/{run_id}`
Получить запись по `run_id` (равен `consultant_agent_logs.id`).

#### `GET /v1/consultant-agent/logs?session_id=...&user_id=...&source_system=...&agent_branch=...&limit=100&offset=0`
Список записей с опциональными фильтрами, отсортированных по `question_dttm` DESC.

#### `PATCH /v1/consultant-agent/logs/{run_id}/answer`
Заполнить ответ для ранее созданной записи.

```json
{
  "agent_answer": "Лимит 500 млн на 90 дней.",
  "answer_dttm": "2026-05-04T12:30:00+00:00",
  "agent_branch": "limits"
}
```

---

### Cleanup

#### `DELETE /v1/admin/truncate/{table_name}`
Очистить таблицу (TRUNCATE CASCADE). Возвращает список каскадно затронутых таблиц.

Допустимые значения `table_name`:

| Таблица | Каскадно затронет |
|---|---|
| `threads` | messages, deal_parsing_log, deal_snapshots |
| `messages` | deal_parsing_log, deal_snapshots |
| `deal_parsing_log` | deal_snapshots |
| `approve_tasks` | approve_deal_snapshots |
| `deal_snapshots` | — |
| `outbox_tasks` | — |
| `approve_deal_snapshots` | — |

Пример ответа:

```json
{
  "truncated": "messages",
  "cascaded": ["deal_parsing_log", "deal_snapshots"]
}
```

---

## Гарантии

**Идемпотентность** — повторный запрос с тем же `message_id` или `task_key` не создаёт дубликат, возвращает существующую запись.

**Атомарность** — `ingest` и `outgoing` пишут сообщение и задачу в одной транзакции. Нет ситуации «письмо есть, задачи нет».

**Параллельные воркеры** — `dequeue` использует `SELECT FOR UPDATE SKIP LOCKED`, несколько воркеров могут читать очередь одновременно без конфликтов.

---

## Структура

```text
db_app/
├── main.py                   # Entry point
├── description.yaml          # Манифест деплоя
├── requirements.txt
│
├── app/
│   ├── main.py               # FastAPI + lifespan
│   ├── config.py             # Pydantic-settings (DB_* env vars)
│   ├── logging_config.py     # structlog
│   ├── dependencies.py       # SessionDep для FastAPI DI
│   │
│   ├── db/
│   │   ├── base.py           # DeclarativeBase
│   │   ├── session.py        # TOML config, engine, async_session
│   │   └── models/
│   │       ├── thread.py
│   │       ├── message.py
│   │       ├── outbox_task.py
│   │       ├── approve_deal_snapshot.py
│   │       ├── approve_task.py
│   │       ├── deal_parsing_log.py
│   │       └── deal_snapshot.py
│   │
│   ├── repositories/
│   │   ├── thread_repo.py              # get_or_create
│   │   ├── message_repo.py             # upsert (INSERT ON CONFLICT DO NOTHING)
│   │   ├── outbox_repo.py              # dequeue() + update_status()
│   │   ├── approve_snapshot_repo.py    # create + get + get_by_task_id
│   │   ├── approve_task_repo.py        # create + get + get_by_calculation_id + update_status + list_filtered
│   │   ├── deal_parsing_log_repo.py    # create + get + list_by_message
│   │   └── deal_snapshot_repo.py       # create + get + list_by_thread
│   │
│   └── api/v1/
│       ├── router.py
│       ├── schemas.py        # Pydantic schemas
│       ├── messages.py
│       ├── outbox.py
│       ├── approve.py        # approve snapshots + tasks
│       ├── deals.py          # parsing log + deal snapshots
│       └── cleanup.py        # TRUNCATE таблиц
│
└── migration/
    └── db1/
        └── changelog.sql     # Liquibase миграции
```

---

## Сценарий: приход и обработка входящего письма

Пошаговая имитация полного цикла: письмо пришло → распарсено → сделка создана → ответ отправлен.

### Шаг 1. Входящее письмо (IMAP worker сохраняет письмо)

`POST /v1/messages/ingest`

```json
{
  "message_id": "incoming-101@client.com",
  "author_email": "treasurer@clientbank.ru",
  "sender_name": "Петров Алексей",
  "subject": "Размещение депозита",
  "body_text": "Добрый день! Хотим разместить 200 млн руб. на 180 дней. ИНН 7707083893. Желаемая ставка 16%.",
  "headers_json": {},
  "thread_id": "incoming-101@client.com",
  "task_key": "ingest:incoming-101@client.com"
}
```

Ответ содержит `task_id` — задача PROCESS_INCOMING в очереди.

### Шаг 2. Воркер забирает задачу из очереди

`POST /v1/outbox/dequeue`

```json
{
  "task_type": "PROCESS_INCOMING",
  "limit": 1,
  "claim_ttl_seconds": 300
}
```

Из ответа запоминаем `id` (task_id) и `claim_token`.

### Шаг 3. Парсинг письма (агент разбирает текст на фрагменты)

`POST /v1/deals/parsing-log`

```json
{
  "message_id": "incoming-101@client.com",
  "fragment_idx": 0,
  "fragment_text": "Хотим разместить 200 млн руб. на 180 дней. ИНН 7707083893. Желаемая ставка 16%.",
  "extraction_status": "success",
  "intent": "new",
  "deal_number": 1,
  "parsed_conditions": {
    "product": "deposit",
    "inn": "7707083893",
    "term_days": 180,
    "volume": 200000000,
    "request_rate": 16.0
  }
}
```

Из ответа запоминаем `id` — это `parsing_log_id` для следующего шага.

### Шаг 4. Создание snapshot сделки (агент рассчитал ставки)

`POST /v1/deals/snapshots`

```json
{
  "thread_id": "incoming-101@client.com",
  "deal_number": 1,
  "iteration": 1,
  "incoming_conditions": {
    "product": "deposit",
    "inn": "7707083893",
    "term_days": 180,
    "volume": 200000000
  },
  "request_rate": 16.0,
  "limit_rate": 15.2,
  "avg_hist_rate": 14.0,
  "model_rate": 14.8,
  "policy_rate": 15.0,
  "final_rate": 15.0,
  "proposed_conditions": {
    "rate": 15.0,
    "term_days": 180,
    "volume": 200000000
  },
  "status": "NEGOTIATING",
  "parsing_log_id": "id-из-шага-3",
  "utilizes_limit": true,
  "agent_text": "Благодарим за обращение. По вашему запросу на размещение 200 млн руб. на 180 дней мы можем предложить ставку 15.0% годовых."
}
```

### Шаг 5. Отправка ответа (агент формирует исходящее письмо)

`POST /v1/messages/outgoing`

```json
{
  "message_id": "reply-101@ourbank.ru",
  "author_email": "deals@ourbank.ru",
  "subject": "Re: Размещение депозита",
  "body_text": "Добрый день! Благодарим за обращение. По вашему запросу на размещение 200 млн руб. на 180 дней мы можем предложить ставку 15.0% годовых.",
  "headers_json": {
    "In-Reply-To": "incoming-101@client.com",
    "References": "incoming-101@client.com"
  },
  "thread_id": "incoming-101@client.com",
  "task_key": "send:reply-101@ourbank.ru",
  "smtp_payload": {
    "to": "treasurer@clientbank.ru",
    "from": "deals@ourbank.ru"
  }
}
```

### Шаг 6. Закрываем задачу PROCESS_INCOMING

`PATCH /v1/outbox/{task_id-из-шага-2}/status`

```json
{
  "status": "DONE",
  "claim_token": "claim_token-из-шага-2"
}
```

### Шаг 7. Воркер забирает задачу SEND_SMTP

`POST /v1/outbox/dequeue`

```json
{
  "task_type": "SEND_SMTP",
  "limit": 1,
  "claim_ttl_seconds": 300
}
```

### Шаг 8. SMTP воркер отправил письмо — закрываем задачу

`PATCH /v1/outbox/{task_id-из-шага-7}/status`

```json
{
  "status": "DONE",
  "claim_token": "claim_token-из-шага-7"
}
```

### Шаг 9. Проверка — все письма в треде

`GET /v1/messages/threads/incoming-101@client.com/messages`

Ожидаем 2 письма: входящее `incoming-101@client.com` и исходящее `reply-101@ourbank.ru`.
