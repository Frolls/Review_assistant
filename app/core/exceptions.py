from __future__ import annotations


class LLMError(Exception):
    code = "llm_error"

    def __init__(self, message: str = "LLM provider request failed.") -> None:
        super().__init__(message)
        self.message = message


class LLMRateLimitError(LLMError):
    code = "llm_rate_limit"

    def __init__(self, message: str = "LLM provider rate limit exceeded.") -> None:
        super().__init__(message)


class LLMTimeoutError(LLMError):
    code = "llm_timeout"

    def __init__(self, message: str = "LLM provider request timed out.") -> None:
        super().__init__(message)


class LLMAuthError(LLMError):
    code = "llm_auth"

    def __init__(self, message: str = "LLM provider authentication failed.") -> None:
        super().__init__(message)


class LLMEmptyResponseError(LLMError):
    code = "llm_empty_response"

    def __init__(self, message: str = "LLM provider returned an empty response.") -> None:
        super().__init__(message)
