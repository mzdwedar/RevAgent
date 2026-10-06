"""What the channel is told about a conclusion (L5, layer 4 narrow).

The facts are ours: the action, the numbers and whether they were planted come from the
stored conclusion, formatted here, and the first line of the message always names
`simulated`. The model is asked for one thing, a short plain-language explanation of those
same numbers, and is shown nothing else: not the hypothesis, not the offer's free text,
nothing a customer or a model wrote. Its words are appended and labelled; they reach no
decision, and a model that is down costs the message its explanation, not its facts.
"""

from __future__ import annotations

from agentstack.context.conclusion_store import StoredConclusion
from agentstack.context.targeting import money

MAX_EXPLANATION = 800

INSTRUCTIONS = (
    "Explain this experiment readout to a product manager in at most three sentences. Use "
    "only the numbers given. Say what the interval means for the decision, and if the "
    "outcomes are simulated, say they are planted and not real customers. Do not recommend "
    "anything beyond the stated action."
)


def _dollars(cents: object) -> str:
    return money(round(float(str(cents))))


def provenance(conclusion: StoredConclusion) -> str:
    return (
        "SIMULATED outcomes (planted, not real customers)"
        if conclusion.simulated
        else "observed outcomes"
    )


def facts(conclusion: StoredConclusion) -> str:
    r = conclusion.readout
    return (
        f"Experiment {conclusion.experiment_id} ({conclusion.experiment_version}), "
        f"{provenance(conclusion)}: the readout says {conclusion.action.upper()}. "
        f"Incremental net saved value {_dollars(r.get('insv_cents', 0))} "
        f"(95% interval {_dollars(r.get('insv_lower_cents', 0))} to "
        f"{_dollars(r.get('insv_upper_cents', 0))}), offer {r.get('offer', 'unknown')}, "
        f"{r.get('treated', '?')} treated and {r.get('control', '?')} control. "
        f"Reason: {conclusion.reason}."
    )


def explanation_context(conclusion: StoredConclusion) -> str:
    """Everything the model is shown: the same numbers, and no free text from anyone."""
    r = conclusion.readout
    return (
        f"action={conclusion.action} simulated={conclusion.simulated} "
        f"insv_cents={r.get('insv_cents')} lower_cents={r.get('insv_lower_cents')} "
        f"upper_cents={r.get('insv_upper_cents')} treated={r.get('treated')} "
        f"control={r.get('control')} endpoint={conclusion.endpoint}"
    )


def message(conclusion: StoredConclusion, explanation: str | None) -> str:
    text = facts(conclusion)
    if explanation and explanation.strip():
        said = explanation.strip()[:MAX_EXPLANATION]
        text += f"\n\nExplanation (model-written; it decided nothing): {said}"
    return text
