"""C2: can each engine say which rule decided, as the audit record needs?"""

from collections import Counter

import cedar_engine
from cases import all_cases, python_verdict
from rego_engine import verdicts

cases = all_cases()
rego = verdicts([c.facts() for c in cases])
multi = Counter()
for c, v in zip(cases, rego, strict=True):
    ok, cedar_reasons, _ = cedar_engine.verdict(c.facts())
    assert cedar_reasons == set(v["deny"]), (c, cedar_reasons, v["deny"])
    if len(cedar_reasons) > 1:
        multi[len(cedar_reasons)] += 1
    _, py_rule = python_verdict(c)
    assert (py_rule == "") == (not cedar_reasons)
denied = sum(1 for v in rego if not v["allow"])
print(f"  {denied} refusals; both engines return the same full reason set for every one")
print(
    f"  refusals with more than one reason: {sum(multi.values())} "
    f"(python's audit names only the first) - {dict(sorted(multi.items()))}"
)
print("  cedar reports policy ids (policy0..N); the rule name needs the @id annotation map")
print("C2 PASS: both give every reason; the first-reason ordering has to live outside Cedar")
