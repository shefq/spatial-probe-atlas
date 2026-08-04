from __future__ import annotations

from typing import Any


class AppError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
        suggested_action: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        self.retryable = retryable
        self.suggested_action = suggested_action


def not_found(kind: str, resource_id: str) -> AppError:
    return AppError(
        f"{kind.upper()}_NOT_FOUND",
        f"The requested {kind.replace('_', ' ')} does not exist.",
        status_code=404,
        details={"id": resource_id},
    )


def state_conflict(message: str, **details: Any) -> AppError:
    return AppError("STATE_CONFLICT", message, status_code=409, details=details)

