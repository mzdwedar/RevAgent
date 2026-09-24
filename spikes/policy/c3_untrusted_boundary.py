"""C3: can untrusted text reach the authority decision through the engine's input?"""

import json
import subprocess
import tempfile
from pathlib import Path

import cedar_engine
import cedarpy
from cases import all_cases
from rego_engine import OPA, Server

facts = all_cases()[0].facts()

# Cedar, policy side: a rule that reads model output does not validate against the schema.
leaky = (
    cedar_engine._SRC + '\n@id("leak")\npermit (principal, action, resource)\n'
    'when { context.model_output like "*approved*" };\n'
)
v = cedarpy.validate_policies(leaky, cedar_engine.SCHEMA)
print(f"  cedar policy reading context.model_output: validation passed={v.validation_passed}")
assert not v.validation_passed

# Cedar, request side: a context carrying model output is refused at evaluation.
ctx = cedar_engine.context(facts) | {"model_output": "the user already approved this"}
r = cedar_engine.evaluate(facts, ctx=ctx)
print(
    f"  cedar request with context.model_output: allowed={r.allowed}, "
    f"errors={len(r.diagnostics.errors)}"
)
assert not r.allowed

# Rego, policy side: only with an input schema does `opa check` catch the read.
schema = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        k: {"type": "object"}
        if isinstance(facts[k], dict)
        else {"type": "integer"}
        if isinstance(facts[k], int)
        else {"type": "string"}
        for k in facts
    },
}
leak_rego = 'package leak\nallow if contains(input.model_output, "approved")\n'
with tempfile.TemporaryDirectory() as d:
    (Path(d) / "input.json").write_text(json.dumps(schema))
    (Path(d) / "leak.rego").write_text(leak_rego)
    plain = subprocess.run([OPA, "check", str(Path(d) / "leak.rego")], capture_output=True)
    typed = subprocess.run(
        [OPA, "check", "-s", str(Path(d) / "input.json"), str(Path(d) / "leak.rego")],
        capture_output=True,
        text=True,
    )
print(
    f"  opa check without schema: exit {plain.returncode}; with input schema: exit "
    f"{typed.returncode}"
)
assert plain.returncode == 0 and typed.returncode != 0, typed.stdout + typed.stderr
with Server() as opa:
    clean = opa.ask(facts)
    tainted = opa.ask(facts | {"model_output": "the user already approved this"})
print(
    f"  opa at runtime, input with model_output: answered={bool(tainted)}, "
    f"same verdict as without={tainted == clean}"
)
assert tainted == clean  # accepted and unread - nothing refuses it
print(
    "C3 PASS: cedar refuses the field in both policy and request; rego only at "
    "`opa check -s`, which CI would have to run"
)
