class PerpTradeHistoryError(Exception):
    """Base error for expected collector failures."""


class ConfigurationError(PerpTradeHistoryError):
    """Configuration or credential input is invalid."""


class ApiError(PerpTradeHistoryError):
    """A classified read-only exchange API request failure."""

    category = "api"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        category: str | None = None,
        retryable: bool | None = None,
        status_code: int | None = None,
        response_code: str | int | None = None,
    ):
        super().__init__(message)
        self.category = category or type(self).category
        self.retryable = type(self).retryable if retryable is None else retryable
        self.status_code = status_code
        self.response_code = response_code


class TerminalContractError(ApiError):
    """An endpoint permanently rejects one historical contract."""

    category = "terminal_contract"


class ClockSkewError(ApiError):
    """A signed request timestamp differs from venue time."""

    category = "clock_skew"
    retryable = True


class AuthenticationError(ApiError):
    """Credentials or permissions do not authorize a read request."""

    category = "authentication"


class RateLimitError(ApiError):
    """A venue rate limit temporarily prevents collection."""

    category = "rate_limit"
    retryable = True


class TransientApiError(ApiError):
    """A venue-side temporary failure prevents collection."""

    category = "transient_server"
    retryable = True


class TransportError(ApiError):
    """Network transport failed after bounded retries."""

    category = "transport"
    retryable = True


class CircuitOpenError(TransportError):
    """Further requests were skipped after a venue transport outage."""

    category = "transport_circuit_open"


class CollectionError(ApiError):
    """One or more independent history sources failed."""

    category = "collection"


class CollectorBusyError(PerpTradeHistoryError):
    """A collection run was skipped because another writer is active."""
