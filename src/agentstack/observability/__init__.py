"""Layer 9 - observability, evaluation, feedback.

Observability produces evidence. Evaluation judges it. They are different jobs and
they live in different places: `Tracer` is for debugging, `AuditSink` is for
accountability, and they are deliberately not the same sink (Part 8).
"""

from agentstack.observability.audit import AuditRecord, AuditSink
from agentstack.observability.spans import (
    REQUIRED_SPANS,
    CollectingSink,
    LoggingSink,
    Span,
    SpanSink,
    Tracer,
    VersionStamp,
)

__all__ = [
    "REQUIRED_SPANS",
    "AuditRecord",
    "AuditSink",
    "CollectingSink",
    "LoggingSink",
    "Span",
    "SpanSink",
    "Tracer",
    "VersionStamp",
]
