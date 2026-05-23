"""Shared exception hierarchy.

RetryableError  — transient failures (network, DB pool exhausted, Claude timeout).
                  Celery will retry these automatically.
NonRetryableError — permanent failures (corrupt file, unsupported format, bad tenant).
                    Celery will NOT retry; task moves to error state immediately.
"""


class EInvError(Exception):
    """Base for all application errors."""


class RetryableError(EInvError):
    """Transient error — safe to retry (network blip, pool exhausted, LLM timeout)."""


class NonRetryableError(EInvError):
    """Permanent error — do not retry (corrupt file, invalid format, bad input)."""


class DocumentNotFoundError(NonRetryableError):
    """Document record missing from DB."""


class UnsupportedFormatError(NonRetryableError):
    """File format not supported for OCR processing."""


class StorageError(RetryableError):
    """MinIO read/write failure."""


class LLMError(RetryableError):
    """Claude API call failed transiently."""


class ValidationFailedError(NonRetryableError):
    """Document failed business-rule validation beyond recoverable threshold."""
