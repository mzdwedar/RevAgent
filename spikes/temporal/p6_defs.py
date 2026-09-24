"""P6: the LangGraph plugin - nodes as activities, interrupt resumed by an Update (cf. ADR-0006)."""

from typing import TypedDict

import langgraph.checkpoint.memory
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from temporalio import workflow
from temporalio.contrib.langgraph import graph
from workflows import _append


class S(TypedDict):
    tag: str
    decision: str


async def draft(s: S) -> dict:
    _append({"tag": s["tag"], "what": "draft"})
    return {}


async def approve(s: S) -> dict:
    _append({"tag": s["tag"], "what": "before-interrupt"})
    d = interrupt("approve?")
    return {"decision": d}


async def commit(s: S) -> dict:
    _append({"tag": s["tag"], "what": f"commit:{s['decision']}"})
    return {}


g = StateGraph(S)
for name, fn in (("draft", draft), ("approve", approve), ("commit", commit)):
    g.add_node(name, fn, metadata={"execute_in": "activity"})
g.add_edge(START, "draft")
g.add_edge("draft", "approve")
g.add_edge("approve", "commit")
g.add_edge("commit", END)


@workflow.defn
class Turn:
    def __init__(self) -> None:
        self.answer: str | None = None

    @workflow.run
    async def run(self, tag: str) -> str:
        app = graph("turn").compile(checkpointer=langgraph.checkpoint.memory.InMemorySaver())
        cfg = {"configurable": {"thread_id": tag}}
        await app.ainvoke({"tag": tag, "decision": ""}, cfg)
        await workflow.wait_condition(lambda: self.answer is not None)
        out = await app.ainvoke(Command(resume=self.answer), cfg)
        return out["decision"]

    @workflow.signal
    def respond(self, answer: str) -> None:
        self.answer = answer
