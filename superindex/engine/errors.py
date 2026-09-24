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
