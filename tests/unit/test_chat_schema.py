import pytest
from pydantic import ValidationError

from app.schemas.chat import MAX_MESSAGE_CONTENT_LENGTH, ChatMessage


def test_chat_message_rejects_empty_content():
    with pytest.raises(ValidationError):
        ChatMessage(role="user", content="")


def test_chat_message_rejects_too_long_content():
    with pytest.raises(ValidationError):
        ChatMessage(role="user", content="x" * (MAX_MESSAGE_CONTENT_LENGTH + 1))


def test_chat_message_repr_redacts_pii():
    message = ChatMessage(
        role="user",
        content="email ivan@example.com phone +7 999 123-45-67",
    )

    representation = repr(message)

    assert "ivan@example.com" not in representation
    assert "+7 999 123-45-67" not in representation
    assert "[EMAIL]" in representation
    assert "[PHONE_RU]" in representation
