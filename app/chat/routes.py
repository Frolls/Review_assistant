from __future__ import annotations

import json
import inspect
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from openai import APIStatusError, BadRequestError, OpenAIError
from pydantic import BaseModel

from app.chat.deps import ChatServiceDep
from app.chat.domain import Chat, ChatMessage
from app.chat.media import media_to_part


router = APIRouter(prefix="/chats", tags=["stateful-chat"])


class CreateChatIn(BaseModel):
    owner_external_id: str
    interface: str
    system_prompt: str | None = None


class CreateChatOut(BaseModel):
    chat_id: UUID


@router.post("", response_model=CreateChatOut)
async def create_chat(payload: CreateChatIn, chat_service: ChatServiceDep) -> CreateChatOut:
    chat = await chat_service.create_chat(
        owner_external_id=payload.owner_external_id,
        interface=payload.interface,
        system_prompt=payload.system_prompt,
    )
    return CreateChatOut(chat_id=chat.id)


@router.get("/{chat_id}", response_model=Chat)
async def get_chat(chat_id: UUID, chat_service: ChatServiceDep) -> Chat:
    chat = await chat_service.get_chat(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    return chat


@router.post("/{chat_id}/messages")
async def send_message(
    chat_id: UUID,
    *,
    content: str = Form(...),
    media: UploadFile | None = File(None),
    chat_service: ChatServiceDep,
) -> StreamingResponse:
    if await chat_service.get_chat(chat_id) is None:
        raise HTTPException(status_code=404, detail="Chat not found")

    check_input = getattr(chat_service, "check_input", None)
    input_moderation_checked = False
    if check_input is not None:
        await check_input(chat_id, content)
        input_moderation_checked = True

    local_response = _local_response(content)
    if local_response is not None and media is None:
        async def local_stream() -> AsyncIterator[str]:
            yield _format_sse_event({"type": "token", "delta": local_response})
            yield _format_sse_event({"type": "done"})

        return StreamingResponse(local_stream(), media_type="text/event-stream")

    refusal = _domain_refusal(content)
    if (
        refusal is not None
        and media is None
        and not await _has_recent_media_context(chat_service, chat_id)
    ):
        async def refusal_stream() -> AsyncIterator[str]:
            yield _format_sse_event({"type": "token", "delta": refusal})
            yield _format_sse_event({"type": "done"})

        return StreamingResponse(refusal_stream(), media_type="text/event-stream")

    media_ref = None
    if media is not None:
        try:
            media_part = await media_to_part(media)
        except ValueError as exc:
            raise HTTPException(status_code=415, detail=str(exc)) from exc
        except OpenAIError as exc:
            raise HTTPException(
                status_code=502,
                detail=_media_processing_error_message(exc),
            ) from exc
        media_ref = {
            "mime": media.content_type,
            "size": media.size,
            "filename": media.filename,
            "part": media_part,
        }

    async def event_stream() -> AsyncIterator[str]:
        saved_message_id: UUID | None = None

        def remember_saved_message(message: ChatMessage) -> None:
            nonlocal saved_message_id
            saved_message_id = message.id

        try:
            send_kwargs: dict[str, Any] = {"media_ref": media_ref}
            send_signature = inspect.signature(chat_service.send_message)
            if "input_moderation_checked" in send_signature.parameters:
                send_kwargs["input_moderation_checked"] = input_moderation_checked
            if "on_message_saved" in send_signature.parameters:
                send_kwargs["on_message_saved"] = remember_saved_message
            async for chunk in chat_service.send_message(chat_id, content, **send_kwargs):
                yield _format_sse_event({"type": "token", "delta": chunk})
        except BadRequestError as exc:
            yield _format_sse_event({"type": "error", "message": _llm_bad_request_message(exc)})
            return
        done_payload = {"type": "done"}
        if saved_message_id is not None:
            done_payload["message_id"] = str(saved_message_id)
        yield _format_sse_event(done_payload)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/{chat_id}/messages", response_model=list[ChatMessage])
async def list_messages(
    chat_id: UUID,
    chat_service: ChatServiceDep,
    limit: int = 50,
) -> list[ChatMessage]:
    return await chat_service.list_messages(chat_id, limit)


@router.delete("/{chat_id}/messages")
async def clear_messages(chat_id: UUID, chat_service: ChatServiceDep) -> dict[str, str]:
    await chat_service.clear_history(chat_id)
    return {"status": "ok"}


def _format_sse_event(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _domain_refusal(content: str) -> str | None:
    text = content.lower()
    entertainment_markers = (
        "анекдот",
        "байк",
        "басн",
        "шутк",
        "пошути",
        "стих",
        "истори",
        "рассказ",
        "сказк",
        "притч",
        "развесели",
        "мем",
    )
    request_markers = (
        "расскажи",
        "поведай",
        "придумай",
        "сочини",
        "напиши",
        "дай",
        "можешь",
    )
    if any(marker in text for marker in entertainment_markers) and any(
        marker in text for marker in request_markers
    ):
        return _domain_refusal_text()
    if not _looks_like_review_request(text):
        return _domain_refusal_text()
    return None


def _local_response(content: str) -> str | None:
    text = content.strip().lower().replace("ё", "е")
    identity_questions = {
        "ты кто",
        "ты кто?",
        "кто ты",
        "кто ты?",
        "что ты умеешь",
        "что ты умеешь?",
        "что умеешь",
        "что умеешь?",
    }
    if text in identity_questions:
        return (
            "Я Telegram-интерфейс ИИ-ассистента для ревью кода. "
            "Помогаю с Python, Ansible, pull request'ами, тестами, "
            "читаемостью и архитектурными замечаниями. Можешь отправить "
            "текст, код, diff, PDF/DOCX или изображение с кодом."
        )
    return None


def _looks_like_review_request(text: str) -> bool:
    domain_markers = (
        "ansible",
        "ansible-lint",
        "molecule",
        "collection",
        "collections",
        "arg spec",
        "arg specs",
        "argspec",
        "argspecs",
        "argument spec",
        "argument specs",
        "module argument",
        "python",
        "питон",
        "пайтон",
        "yaml",
        "jinja",
        "pytest",
        "pip",
        "venv",
        "virtualenv",
        "poetry",
        "uv",
        "ruff",
        "mypy",
        "pyproject",
        "requirements",
        "fastapi",
        "sqlalchemy",
        "redis",
        "docker",
        "kubernetes",
        "код",
        "кода",
        "ревью",
        "проверь",
        "проверить",
        "ошибк",
        "баг",
        "фикс",
        "рефактор",
        "тест",
        "архитект",
        "читаем",
        "поддерживаем",
        "pull request",
        "merge request",
        "diff",
        "pr",
        "traceback",
        "stacktrace",
        "exception",
        "playbook",
        "role",
        "roles/",
        "tasks:",
        "- name:",
        "def ",
        "class ",
        "import ",
        "from ",
    )
    return any(marker in text for marker in domain_markers)


def _domain_refusal_text() -> str:
    return (
        "Я помогаю только с ревью Python/Ansible-кода, pull request'ов, "
        "тестов, читаемости и архитектуры. Пришли код, diff, описание PR "
        "или документ в этих рамках, и я разберу его по делу."
    )


async def _has_recent_media_context(chat_service: ChatServiceDep, chat_id: UUID) -> bool:
    messages = await chat_service.list_messages(chat_id, limit=10)
    return any(message.media_refs is not None for message in messages)


def _llm_bad_request_message(exc: BadRequestError) -> str:
    message = str(exc)
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            message = response.text
        except Exception:
            message = str(exc)
    if "does not support multimodal" in message.lower():
        return (
            "Текущая модель не поддерживает изображения. "
            "Для проверки фото переключите backend на vision-модель в Ollama "
            "или отправьте текст/документ."
        )
    return f"LLM backend отклонил запрос: {message[:500]}"


def _media_processing_error_message(exc: OpenAIError) -> str:
    message = str(exc)
    if isinstance(exc, APIStatusError):
        message = str(exc.response.text)
    if "audio" in message.lower() or "transcription" in message.lower():
        return (
            "Не удалось расшифровать голосовое сообщение. "
            "Для voice нужен backend с OpenAI-compatible endpoint "
            "`/audio/transcriptions` для Whisper; текущий Ollama endpoint, "
            "скорее всего, его не поддерживает."
        )
    return "Не удалось обработать медиафайл. Попробуйте другой файл или отправьте текст."
