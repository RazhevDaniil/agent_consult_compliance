from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.v1.schemas import (
    IngestMessageIn,
    IngestOut,
    MessageOut,
    OutgoingMessageIn,
    OutgoingOut,
    ThreadOut,
)
from ...dependencies import SessionDep
from ...logging_config import get_logger
from ...repositories.message_repo import MessageRepo
from ...repositories.outbox_repo import OutboxRepo
from ...repositories.thread_repo import ThreadRepo

log = get_logger("app.api.messages")

router = APIRouter(prefix="/messages", tags=["messages"])


def _make_thread_repo(session: AsyncSession) -> ThreadRepo:
    return ThreadRepo(session)


def _make_message_repo(session: AsyncSession) -> MessageRepo:
    return MessageRepo(session)


def _make_outbox_repo(session: AsyncSession) -> OutboxRepo:
    return OutboxRepo(session)


@router.post("/ingest", response_model=IngestOut, status_code=201)
async def ingest_message(
    body: IngestMessageIn,
    session: SessionDep,
) -> IngestOut:
    """Save an incoming email and atomically enqueue a PROCESS_INCOMING task."""
    log.info(
        "ingest.start",
        message_id=body.message_id,
        thread_id=body.thread_id,
        author_email=body.author_email,
        task_key=body.task_key,
    )
    thread_repo = _make_thread_repo(session)
    message_repo = _make_message_repo(session)
    outbox_repo = _make_outbox_repo(session)

    thread, thread_created = await thread_repo.get_or_create(
        thread_id=body.thread_id,
        subject=body.subject,
    )
    log.debug(
        "ingest.thread_resolved",
        thread_id=thread.thread_id,
        created=thread_created,
    )

    message, msg_created = await message_repo.upsert(
        message_id=body.message_id,
        thread_id=thread.thread_id,
        author_email=body.author_email,
        body_text=body.body_text,
        body_html=body.body_html,
        headers_json=body.headers_json,
        run_id=body.run_id,
    )
    log.debug(
        "ingest.message_resolved",
        message_id=message.message_id,
        created=msg_created,
    )

    process_payload = {
        "message_id": message.message_id,
        "thread_id": thread.thread_id,
        "author_email": body.author_email,
        "sender_name": body.sender_name,
        "reply_to_email": body.reply_to_email,
        "subject": body.subject,
        "body": body.body_text or body.body_html or "",
        "run_id": body.run_id,
    }
    task, task_created = await outbox_repo.create(
        task_type="PROCESS_INCOMING",
        task_key=body.task_key,
        email_id=message.message_id,
        payload_json=process_payload,
        run_id=body.run_id,
    )
    log.debug(
        "ingest.task_resolved",
        task_id=task.id,
        task_key=body.task_key,
        created=task_created,
    )

    await session.commit()

    log.info(
        "ingest.done",
        message_id=message.message_id,
        task_id=task.id,
        task_created=task_created,
        msg_created=msg_created,
    )
    return IngestOut(
        message=MessageOut.model_validate(message),
        thread=ThreadOut.model_validate(thread),
        task_id=task.id,
        task_created=task_created,
    )


@router.post("/outgoing", response_model=OutgoingOut, status_code=201)
async def save_outgoing_message(
    body: OutgoingMessageIn,
    session: SessionDep,
) -> OutgoingOut:
    """Save an outgoing email and atomically enqueue a SEND_SMTP task."""
    log.info(
        "outgoing.start",
        message_id=body.message_id,
        thread_id=body.thread_id,
        author_email=body.author_email,
        task_key=body.task_key,
    )
    thread_repo = _make_thread_repo(session)
    message_repo = _make_message_repo(session)
    outbox_repo = _make_outbox_repo(session)

    thread, thread_created = await thread_repo.get_or_create(
        thread_id=body.thread_id,
        subject=body.subject,
    )
    log.debug(
        "outgoing.thread_resolved",
        thread_id=thread.thread_id,
        created=thread_created,
    )

    message, msg_created = await message_repo.upsert(
        message_id=body.message_id,
        thread_id=thread.thread_id,
        author_email=body.author_email,
        body_text=body.body_text,
        body_html=body.body_html,
        headers_json=body.headers_json,
        run_id=body.run_id,
    )
    log.debug(
        "outgoing.message_resolved",
        message_id=message.message_id,
        created=msg_created,
    )

    payload = dict(body.smtp_payload or {})
    # Если caller передал run_id явным полем, а в smtp_payload его нет —
    # подмешиваем, чтобы SMTP-воркер мог восстановить контекст без двух источников правды.
    if body.run_id and "run_id" not in payload:
        payload["run_id"] = body.run_id
    task, task_created = await outbox_repo.create(
        task_type="SEND_SMTP",
        task_key=body.task_key,
        email_id=message.message_id,
        payload_json=payload or None,
        run_id=body.run_id,
    )
    log.debug(
        "outgoing.task_resolved",
        task_id=task.id,
        task_key=body.task_key,
        created=task_created,
    )

    await session.commit()

    log.info(
        "outgoing.done",
        message_id=message.message_id,
        task_id=task.id,
        task_created=task_created,
        msg_created=msg_created,
    )
    return OutgoingOut(
        message=MessageOut.model_validate(message),
        thread=ThreadOut.model_validate(thread),
        task_id=task.id,
        task_created=task_created,
    )


@router.get("/by-author/{author_email}", response_model=list[MessageOut])
async def list_by_author(
    author_email: str,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[MessageOut]:
    log.info("list_by_author.start", author_email=author_email, limit=limit, offset=offset)
    repo = MessageRepo(session)
    messages = await repo.list_by_author(author_email, limit=limit, offset=offset)
    log.info("list_by_author.done", author_email=author_email, count=len(messages))
    return [MessageOut.model_validate(m) for m in messages]


@router.get("/threads/{thread_id}/messages", response_model=list[MessageOut])
async def list_thread_messages(
    thread_id: str,
    session: SessionDep,
) -> list[MessageOut]:
    log.info("list_thread_messages.start", thread_id=thread_id)
    thread_repo = ThreadRepo(session)
    thread = await thread_repo.get(thread_id)
    if thread is None:
        log.warning("list_thread_messages.thread_not_found", thread_id=thread_id)
        raise HTTPException(status_code=404, detail="Thread not found")

    repo = MessageRepo(session)
    messages = await repo.list_by_thread(thread_id)
    log.info("list_thread_messages.done", thread_id=thread_id, count=len(messages))
    return [MessageOut.model_validate(m) for m in messages]
