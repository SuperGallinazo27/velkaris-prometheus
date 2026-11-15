class AnalysisError(Exception):
    """A controlled error. `extra` adds machine-readable fields next to code and message in the
    response detail (for example, the busy ranges of a place); it never replaces them."""

    def __init__(self, status_code: int, code: str, message: str, extra: dict | None = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra or {}
        super().__init__(message)
