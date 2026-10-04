"""Safe failure context without request bodies, credentials, or URL queries."""

from __future__ import annotations

from pathlib import Path
import traceback

import httpx


def error_context(exc: BaseException) -> str:
    fields = [f"error={type(exc).__name__}"]
    if isinstance(exc, httpx.HTTPError):
        try:
            request = exc.request
        except RuntimeError:
            request = None
        if request is not None:
            fields.append(f"host={request.url.host}")
        if isinstance(exc, httpx.HTTPStatusError):
            fields.append(f"status={exc.response.status_code}")
    cause = exc.__cause__
    if cause is not None:
        fields.append(f"cause={type(cause).__name__}")
    frames = traceback.extract_tb(exc.__traceback__)
    if frames:
        # The source call site helps identify adapter bugs without printing
        # exception messages, source text, or secrets embedded in URLs.
        frame = next((frame for frame in frames if frame.name in {
            "fetch", "fetch_mode", "fetch_category",
        }), frames[-1])
        fields.append(f"origin={Path(frame.filename).name}:{frame.lineno}")
    return " ".join(fields)
