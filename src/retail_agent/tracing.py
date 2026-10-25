"""JSONL trace writer (docs/contracts.md §1.3, DD §15). One record per line, schema version 1."""
from __future__ import annotations

import copy
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Literal, Mapping

SCHEMA_VERSION = 1

EVENTS = frozenset({
    "session", "node", "llm_call", "tool_call", "security_event", "pii_in_input",
    "deletion", "feedback", "turn_summary",
})

SECURITY_KINDS = frozenset({
    "pii_column", "out_of_scope_brand", "quasi_identifier", "qualified_table", "forbidden_statement",
    "forbidden_function", "information_schema", "reserved_cte_name", "repeated_pii_attempt",
    "pii_in_result", "pii_in_answer", "thread_access_denied", "report_access_denied", "malicious_intent",
})

TURN_OUTCOMES = frozenset({
    "answered", "partial", "cannot_answer", "clarification", "refused", "schema_answer", "library",
    "confirm_pending", "deleted", "delete_cancelled", "no_access", "error",
})

ERROR_TYPES = frozenset({
    "quota_exhausted", "llm_unavailable", "llm_timeout", "llm_request", "llm_output", "deadline",
    "budget_exhausted", "bq_unavailable", "bq_timeout", "bq_bad_request", "bytes_cap", "validation", "internal",
})

CONTEXT_KEYS = ("thread_id", "turn_id", "exec_id", "persona_hash", "node")
_ID_KEYS = ("thread_id", "turn_id", "exec_id")        # always present, null before login
_TRUNCATED_FIELDS = ("prompt", "response", "sql")
MIN_SECRET_LEN = 8
REDACTED = "<REDACTED>"


class _FileSink:
    """Append-only JSONL file shared by a tracer and all its bound children."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock = threading.Lock()

    def append(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()


class Tracer:
    def __init__(self, path: Path, *, max_field_chars: int = 20000,
                 secret_values: Iterable[str] = ()) -> None:
        self.max_field_chars = max_field_chars
        self._secrets = tuple(sorted({s for s in secret_values if s and len(s) >= MIN_SECRET_LEN},
                                     key=len, reverse=True))
        self._sink: Any = _FileSink(path)
        self._context: dict[str, str | None] = {}

    @property
    def path(self) -> Path | None:
        return getattr(self._sink, "path", None)

    def bind(self, **context: str | None) -> "Tracer":
        """Child tracer sharing the file, with thread_id/turn_id/exec_id/persona_hash/node merged."""
        unknown = set(context) - set(CONTEXT_KEYS)
        if unknown:
            raise ValueError(f"Unknown trace context keys: {sorted(unknown)}")
        child = copy.copy(self)
        child._context = {**self._context, **context}
        return child

    def write(self, event: str, **fields: Any) -> dict:
        """Add schema_version, ts and the bound context; truncate long fields (set truncated);
        replace any occurrence of a secret value with '<REDACTED>'; append; return the record."""
        if event not in EVENTS:
            raise ValueError(f"Unknown trace event '{event}'; expected one of {sorted(EVENTS)}")
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "event": event,
        }
        for key in _ID_KEYS:
            record[key] = self._context.get(key)
        for key in CONTEXT_KEYS:
            if key not in _ID_KEYS and self._context.get(key) is not None:
                record[key] = self._context[key]
        record.update(fields)

        for key in _TRUNCATED_FIELDS:
            if key in record and self._truncate(record, key):
                record["truncated"] = True
        record = self._redact(record)
        self._sink.append(record)
        return record

    def security_event(self, kind: str, reason_code: str | None = None, **detail: Any) -> dict:
        if kind not in SECURITY_KINDS:
            raise ValueError(f"Unknown security event kind '{kind}'")
        return self.write("security_event", kind=kind, reason_code=reason_code, detail=detail)

    def pii_in_input(self, counts: Mapping[str, int]) -> dict:
        """Counts per kind only, never the values."""
        return self.write("pii_in_input", counts={k: int(v) for k, v in counts.items()})

    def feedback(self, rating: Literal["good", "bad"], comment: str | None) -> dict:
        return self.write("feedback", rating=rating, comment=comment)

    def turn_summary(self, **fields: Any) -> dict:
        outcome = fields.get("outcome")
        if outcome is not None and outcome not in TURN_OUTCOMES:
            raise ValueError(f"Unknown turn outcome '{outcome}'")
        return self.write("turn_summary", **fields)

    # -- internals ---------------------------------------------------------

    def _truncate(self, record: dict, key: str) -> bool:
        limit = self.max_field_chars
        value = record[key]
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
        if len(text) <= limit:
            return False
        record[key] = text[:limit]
        return True

    def _redact(self, value: Any) -> Any:
        if not self._secrets:
            return value
        if isinstance(value, str):
            for secret in self._secrets:
                value = value.replace(secret, REDACTED)
            return value
        if isinstance(value, dict):
            return {self._redact(k): self._redact(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._redact(v) for v in value]
        return value


class _ListSink:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def append(self, record: dict) -> None:
        self.records.append(record)


class NullTracer(Tracer):
    """Tests: keeps records in .records (list[dict]), writes nothing."""

    def __init__(self, *, max_field_chars: int = 20000, secret_values: Iterable[str] = ()) -> None:
        super().__init__(Path("/dev/null"), max_field_chars=max_field_chars, secret_values=secret_values)
        self._sink = _ListSink()

    @property
    def path(self) -> Path | None:
        return None

    @property
    def records(self) -> list[dict]:
        return self._sink.records


def read_traces(path: Path) -> Iterator[dict]:
    """Yield the records of a trace file; skips malformed lines."""
    path = Path(path)
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                yield record


def validate_record(record: Mapping[str, Any]) -> list[str]:
    """Problems found when checking a record against the contract schema (empty list = valid)."""
    problems: list[str] = []
    if record.get("schema_version") != SCHEMA_VERSION:
        problems.append(f"schema_version must be {SCHEMA_VERSION}")
    ts = record.get("ts")
    if not (isinstance(ts, str) and ts.endswith("Z")):
        problems.append("ts must be a UTC ISO-8601 string ending in 'Z'")
    else:
        try:
            datetime.fromisoformat(ts[:-1] + "+00:00")
        except ValueError:
            problems.append("ts is not parseable")
    event = record.get("event")
    if event not in EVENTS:
        problems.append(f"unknown event {event!r}")
        return problems
    for key in _ID_KEYS:
        if key not in record:
            problems.append(f"missing {key}")
    required = {
        "node": ("node", "latency_ms"),
        "llm_call": ("node", "model", "role", "prompt", "response", "tokens_in", "tokens_out",
                     "latency_ms", "attempt", "retry_count", "fallback_used"),
        "tool_call": ("node", "tool", "query_id", "outcome", "latency_ms"),
        "security_event": ("kind", "reason_code", "detail"),
        "pii_in_input": ("counts",),
        "feedback": ("rating",),
        "turn_summary": ("outcome", "latency_ms", "llm_calls", "tool_calls", "tokens_in", "tokens_out"),
    }.get(event, ())
    problems += [f"{event}: missing {k}" for k in required if k not in record]
    if event == "security_event" and record.get("kind") not in SECURITY_KINDS:
        problems.append(f"security_event: unknown kind {record.get('kind')!r}")
    if event == "turn_summary" and record.get("outcome") not in TURN_OUTCOMES:
        problems.append(f"turn_summary: unknown outcome {record.get('outcome')!r}")
    error_type = record.get("error_type")
    if error_type is not None and error_type not in ERROR_TYPES:
        problems.append(f"unknown error_type {error_type!r}")
    return problems
