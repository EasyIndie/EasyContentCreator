class DomainError(Exception):
    """Base error for failures with defined pipeline semantics."""


class RetryableError(DomainError):
    """A transient external failure that may be retried by the worker."""


class PermanentError(DomainError):
    """A business failure that must not be retried automatically."""


class AdapterContractError(PermanentError):
    """An adapter returned data that violates the project contract."""


class InvalidStateTransition(PermanentError):
    """A requested project state transition is not allowed."""
