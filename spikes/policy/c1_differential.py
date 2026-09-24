"""C1: do Cedar and Rego decide exactly what agentstack.policy decides, on every input?"""

import cedar_engine
import cedarpy
from cases import all_cases, primary, python_verdict
from rego_engine import verdicts

cases = all_cases()
py = [python_verdict(c) for c in cases]
cedar = [cedar_engine.verdict(c.facts()) for c in cases]
rego = verdicts([c.facts() for c in cases])

assert not any(errs for _, _, errs in cedar), "cedar evaluation errors"
cedar_bad = sum((ok, primary(r)) != p for (ok, r, _), p in zip(cedar, py, strict=True))
rego_bad = sum((v["allow"], v["primary"]) != p for v, p in zip(rego, py, strict=True))
print(f"  {len(cases)} cases: cedar disagrees on {cedar_bad}, rego disagrees on {rego_bad}")
assert cedar_bad == 0 and rego_bad == 0

# Teeth: plant the bug the ALWAYS tier exists to prevent - a policy grant satisfying it.
mutant = cedar_engine._SRC.replace(
    '!(context.approval.exists && context.approval.granted_by == "human")',
    "!context.approval.exists",
)
assert mutant != cedar_engine._SRC
cedar_engine.POLICIES = cedarpy.PolicySet.from_str(mutant)
caught = sum(
    (ok, primary(r)) != p
    for (ok, r, _), p in (
        (cedar_engine.verdict(c.facts()), p) for c, p in zip(cases, py, strict=True)
    )
)
print(f"  planted 'policy grant satisfies ALWAYS' in cedar: {caught} disagreements")
assert caught > 0
print(
    "C1 PASS: both engines agree with Python on all 480 inputs, and the test catches a planted bug"
)
