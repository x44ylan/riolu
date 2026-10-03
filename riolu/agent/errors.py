"""OpenCode failures preserved across the local worker API."""


class OpenCodeError(RuntimeError):
    def __init__(self, message: str, *, code: str = "request_failed", status: int = 502):
        super().__init__(message)
        self.code = code
        self.status = status


def failure_reason(messages: list[dict], native: dict) -> str:
    for message in reversed(messages):
        info = message.get("info") or message
        error = info.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:1000]
    if not native.get("model"):
        return "No model was selected. Choose one with /model, then send your prompt again."
    return "The run failed before producing a reply. Check /status or choose another /model."
