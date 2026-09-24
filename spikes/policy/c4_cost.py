"""C4: what does a decision cost, per engine, as each would be deployed?"""

import statistics
import time

import cedar_engine
from cases import all_cases, python_verdict
from rego_engine import Server

facts = [(c, c.facts()) for c in all_cases()]


def timed(fn, reps=3):
    # fn takes (case, facts); each engine reads the one it was built for.
    samples = []
    for _ in range(reps):
        for c, f in facts:
            t = time.perf_counter()
            fn(c, f)
            samples.append((time.perf_counter() - t) * 1e6)
    samples.sort()
    return statistics.median(samples), samples[int(len(samples) * 0.99)]


rows = {
    "python (in-process)": timed(lambda c, _f: python_verdict(c)),
    "cedar  (cedarpy, in-process)": timed(lambda _c, f: cedar_engine.verdict(f)),
}
with Server() as opa:
    rows["rego   (OPA sidecar, HTTP)"] = timed(lambda _c, f: opa.ask(f))
for name, (p50, p99) in rows.items():
    print(f"  {name:<30} p50 {p50:8.1f}us   p99 {p99:8.1f}us")
assert all(p99 < 50_000 for _, p99 in rows.values())
print("C4 PASS: every engine decides well inside one gateway call's database round trip")
