"""Layer 5 - context, retrieval, memory.

Context is not what the model knows. It is the bounded working set assembled for one
turn, derived from the transcript and never mistaken for it. Retrieval supplies
candidates, not authority. Memory is durable state with a lifecycle, and it is written
explicitly or not at all (Part 5).
"""

from agentstack.context.assemble import ContextBundle, assemble
from agentstack.context.items import REQUIRED_ITEM_FIELDS, ContextItem, Scope, Trust
from agentstack.context.memory import MaintenanceQueue, MemoryRecord, MemoryStore, write
from agentstack.context.retrieval import Candidate, Retriever, StaticRetriever

__all__ = [
    "REQUIRED_ITEM_FIELDS",
    "Candidate",
    "ContextBundle",
    "ContextItem",
    "MaintenanceQueue",
    "MemoryRecord",
    "MemoryStore",
    "Retriever",
    "Scope",
    "StaticRetriever",
    "Trust",
    "assemble",
    "write",
]
