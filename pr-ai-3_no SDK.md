# Надежность. Сохранение трейса действий агента

## Назначение
Определить универсальную инструкцию для реализации tracing-слоя, фиксирующего полный жизненный цикл обработки запроса агентом и отправляющего события в Kafka.

Реализация должна быть применима в любом языке и стеке.

—

# Обязательная управляющая инструкция для LLM

В файле `pr-ai-3.md` представлено требование для реализации в проекте.

Сформируй:
- план разработки в файле `./develop.md`;
- фиксируй текущий прогресс по шагам в файле `./progress.md`.

После реализации требований, описанных в файле `pr-ai-3.md`:
- напиши unit-тесты к реализованному функционалу;
- прогресс тестирования фиксируй в файле `progress.md`.

—

# Цель
Реализовать tracing-механизм, который:
1. собирает полный жизненный цикл обработки запроса;
2. фиксирует обязательные атрибуты;
3. формирует span-события;
4. отправляет их в Kafka;
5. интегрирован в реальный execution flow агента;
6. не влияет на выполнение при сбоях.

—

# Ключевое требование (обязательное)

Недостаточно реализовать только классы tracing.

Необходимо:
- встроить tracing в реальные точки входа агента;
- обернуть ВСЕ сценарии запуска агента;
- обеспечить вызов tracer в production-коде, а не только декларацию.

—

# Поддерживаемые сценарии запуска агента

Реализация должна учитывать, что агент может запускаться через разные entrypoints:

- HTTP / REST
- Kafka consumer
- CLI / batch
- internal function call
- scheduler / cron
- другой агент

## Требование

Tracing должен подключаться ко ВСЕМ сценариям запуска.

—

# Архитектура

Реализовать 4 уровня:

1. TraceContext
2. Span (TraceEvent)
3. Tracer
4. Exporter (Kafka)

—

# Конфигурация

## Общая конфигурация tracing

Создать отдельный конфигурационный слой:

```text
TRACING_ENABLED = true
TRACING_EXPORT_ENABLED = true
TRACING_MAX_PAYLOAD_SIZE = 10000
```

—

## Конфигурация Kafka (tracing)

```text
KAFKA_TOPIC = SETTINGS.KAFKA_TOPIC
KAFKA_HOSTS = SETTINGS.KAFKA_HOSTS
```

—

## Конфигурация дополнительной Kafka (если используется)

Если в проекте уже есть Kafka:

* НЕ создавать новый producer;
* использовать существующий механизм.

Если требуется отдельный Kafka-контур:

```text
TRACING_KAFKA_ENABLED = true
TRACING_KAFKA_TOPIC = SETTINGS.TRACING_TOPIC
TRACING_KAFKA_HOSTS = SETTINGS.TRACING_HOSTS
```

—

## Правило выбора Kafka

* если в проекте уже есть Kafka producer → использовать его;
* если нет → создать новый;
* запрещено дублировать producer.

—

# Минимальный набор полей

## Критические

* trace_id
* span_id
* parent_span_id
* operation_uid
* agent_uid
* name
* call_type
* started_at
* finished_at
* status

## Расширенные

* parent_operation_uid
* agent_name
* target_name
* request_payload
* response_payload
* result_payload
* error_message

## Контроль

* hops
* ttl
* stop_event

## Логика

* is_mutation
* rollback_possible
* action_name

—

# API Tracer

```text
startRootSpan(name, context) -> span
startChildSpan(name, parentSpan, call_type, target_name) -> span

setAttribute(span, key, value)
recordError(span, error_message)

finishSpan(span, status, result_payload)
exportSpan(span)
```

—

# Поведение Tracer

* пробрасывает trace_id;
* поддерживает иерархию;
* автоматически считает duration;
* не бросает исключения наружу;
* всегда вызывает exportSpan.

—

# Интеграция в код (обязательная)

## Требование

Tracer ДОЛЖЕН быть встроен в реальные entrypoints:

### Пример (псевдокод)

```text
function handleRequest(input):

context = createTraceContext(input)

root = tracer.startRootSpan(«agent_request», context)

try:

planSpan = tracer.startChildSpan(«planning», root, «internal», null)

// логика планирования

tracer.finishSpan(planSpan, «ok», null)

callSpan = tracer.startChildSpan(«call_service», root, «api_call», «service»)

// внешний вызов

tracer.finishSpan(callSpan, «ok», response)

tracer.finishSpan(root, «ok», result)

catch error:

tracer.recordError(root, error)
tracer.finishSpan(root, «error», null)
```

—

# Обязательные точки внедрения

Tracer должен вызываться в:

* entrypoint агента;
* каждом внешнем вызове;
* каждом вызове LLM;
* каждом межагентном вызове;
* каждом изменении состояния;
* формировании результата;
* обработке ошибок.

—

# Типы call_type

* service_call
* api_call
* agent_call
* llm_call
* action

—

# Mutation правила

```text
если изменяет состояние:
is_mutation = true

если есть откат:
rollback_possible = true

если нет:
rollback_possible = false

если read-only:
is_mutation = false
rollback_possible = null
```

—

# Сериализация

## Требования

* не падать;
* обрезать данные;
* обрабатывать бинарные данные;
* приводить сложные типы к строке.

## Псевдокод

```text
function safeSerialize(value):

try:
json = TO_JSON(value)

if SIZE(json) > LIMIT:
return TRUNCATE(json)

return json

catch:
return STRING(value)
```

—

# Kafka Exporter

## Использование существующего producer

Если в проекте уже есть Kafka producer:

```text
producer.send(topic, message, headers)
```

Использовать именно его.

—

## Если producer отсутствует

Создать минимальный:

```text
kafka.send({
topic: KAFKA_TOPIC,
message: serialized,
headers: {...}
})
```

—

## Headers

```json
{
«cluster-id»: SETTINGS.CLUSTER_ID,
«agent-id»: SETTINGS.AGENT_ID,
«namespace»: SETTINGS.NAMESPACE,
«distributive»: SETTINGS.DISTRIBUTIVE
}
```

—

# Важные ограничения

* запрещено игнорировать типы данных;
* запрещено отключать проверки типов;
* запрещено упрощать сериализацию до «просто строка»;
* запрещено пропускать поля из обязательного набора;
* запрещено оставлять tracer неиспользованным;
* запрещено создавать новый Kafka producer при наличии существующего.

—

# Критерии приемки

* tracing реально вызывается в коде;
* покрыты все entrypoints;
* есть root + child span-ы;
* фиксируются все обязательные поля;
* данные отправляются в Kafka;
* используется существующий producer (если есть);
* ошибки Kafka не ломают выполнение;
* есть unit-тесты;
* прогресс зафиксирован в progress.md.

