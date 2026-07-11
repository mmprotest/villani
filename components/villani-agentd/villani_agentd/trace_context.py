from __future__ import annotations

import os
import re
from typing import Mapping

_TRACEPARENT = re.compile(
    r"^(?P<version>[0-9a-f]{2})-(?P<trace>[0-9a-f]{32})-(?P<span>[0-9a-f]{16})-(?P<flags>[0-9a-f]{2})$"
)


def parse_traceparent(value: str | None) -> tuple[str, str] | None:
    match = _TRACEPARENT.fullmatch((value or "").strip())
    if not match or match.group("version") == "ff":
        return None
    trace_id, span_id = match.group("trace"), match.group("span")
    if set(trace_id) == {"0"} or set(span_id) == {"0"}:
        return None
    return trace_id, span_id


def propagated_environment(
    trace_id: str, span_id: str, run_id: str, environ: Mapping[str, str] | None = None
) -> tuple[dict[str, str], str, str | None]:
    output = dict(os.environ if environ is None else environ)
    existing_key = next((key for key in output if key.lower() == "traceparent"), None)
    existing = output.get(existing_key) if existing_key else None
    parsed = parse_traceparent(existing)
    if parsed:
        effective_trace, parent_span = parsed
    else:
        effective_trace, parent_span = trace_id, None
        if existing_key and existing_key != "traceparent":
            output.pop(existing_key, None)
        output["traceparent"] = f"00-{trace_id}-{span_id}-01"
    output["VILLANI_RUN_ID"] = run_id
    output["VILLANI_TRACE_ID"] = effective_trace
    output["VILLANI_PARENT_SPAN_ID"] = parent_span or span_id
    return output, effective_trace, parent_span
