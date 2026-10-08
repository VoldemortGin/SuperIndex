import re


class SuperIndexAPIError(Exception):
    """status_code carries the HTTP status when the raising site passes it;
    None does not imply local/client-side."""

    def __init__(self, *args: object, status_code: int | None = None) -> None:
        super().__init__(*args)
        self.status_code = status_code


def _superindex_cause(exc: BaseException | None) -> SuperIndexAPIError | None:
    """The SuperIndexAPIError behind a framework's wrapper exception, if any."""
    while exc is not None:
        if isinstance(exc, SuperIndexAPIError):
            return exc
        exc = exc.__cause__
    return None


# A rate-limit / quota error, from its text: 429 only as "code / status / HTTP
# 429" or "429 Too Many Requests", so page numbers and the like do not match.
RATE_LIMIT_RE = re.compile(
    r"rate[ _-]?limit|too many requests|throttl|quota|request_limit_exceeded|resource[ _]exhausted"
    r"|(?:code|status)\W{0,4}429(?!\d)|\bhttp[\w/.]{0,8}\s429(?!\d)|(?<![\w.])429\s+(?:too many|client error)"
    r"|\b(?:tpm|rpm)\b|tokens per min|requests per min",
    re.IGNORECASE)


def is_rate_limit_error(error: BaseException | str | None) -> bool:
    """Whether an exception (status 429, or its "Type: message" text) or an
    error text is a model-API rate-limit / quota error."""
    if isinstance(error, BaseException):
        if getattr(error, "status_code", None) == 429:
            return True
        error = f"{type(error).__name__}: {error}"
    return bool(error) and bool(RATE_LIMIT_RE.search(error))
