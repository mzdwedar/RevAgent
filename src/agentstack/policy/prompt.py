"""What the approver actually reads (Part 7).

"Approval should be specific enough that a person can inspect the action." A vague
"proceed with task?" trains people to click through; so does a technically complete
line that a tired person cannot parse. This module exists because the prompt is a
designed artifact, not an f-string.

Three things it gets right that the f-string got wrong:

* **Both identities are named.** Who will act is not who asked, and neither is the
  approver reading this. All three are different people in the case that matters.
* **Irreversible says what reversing costs.** The word lands only with its remedy.
* **Free text is labelled and defanged.** Payload strings are model-influenced, and
  the approval prompt is the single place an injection most wants to reach. They are
  marked untrusted, escaped so they cannot forge a field line, and truncated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agentstack.tools.spec import ActsAs, ToolSpec

_MAX_TEXT = 160
_STRUCTURAL_TYPES = {"integer", "number", "boolean"}


@dataclass(frozen=True, slots=True)
class ApprovalPrompt:
    spec: ToolSpec
    resource: str
    payload: dict[str, Any]
    principal: str
    acts_as: ActsAs
    requested_by: str
    channel: str

    def headline(self) -> str:
        reversibility = "reversible" if self.spec.reversible else "IRREVERSIBLE"
        return f"{self.spec.name} — {reversibility}"

    def render(self) -> str:
        lines = [self.headline()]
        for label, value in self._fields():
            lines.append(f"  {label:<11} {value}")
        return "\n".join(lines)

    def _fields(self) -> list[tuple[str, str]]:
        fields: list[tuple[str, str]] = []
        for name, raw in sorted(self.payload.items()):
            fields.append((name, self._render_value(name, raw)))
        fields.append(("resource", self.resource))
        fields.append(("surface", self.spec.surface.value))
        fields.append(("acting as", f"{self.principal} ({self.acts_as.value})"))
        fields.append(("requested by", f"{self.requested_by} via {self.channel}"))
        if not self.spec.reversible:
            fields.append(("to reverse", self.spec.reversal_note))
        return fields

    def _render_value(self, name: str, raw: Any) -> str:
        declared: dict[str, Any] = self.spec.input_schema.get("properties", {}).get(name, {})
        if declared.get("type") in _STRUCTURAL_TYPES:
            return str(raw)
        text = str(raw)
        if declared.get("type") == "string" and declared.get("format") == "id":
            return text
        return f"{_defang(text)}  [untrusted text, from the model's proposal]"


def _defang(text: str) -> str:
    """Strip the structure a payload string could otherwise forge."""
    flattened = text.replace("\r", " ").replace("\n", " ⏎ ").strip()
    if len(flattened) > _MAX_TEXT:
        flattened = f"{flattened[:_MAX_TEXT]}…"
    return f'"{flattened}"'
