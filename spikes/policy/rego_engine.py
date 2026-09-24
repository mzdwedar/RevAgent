"""Evaluate cases with OPA: batch via `opa eval`, or per call against `opa run --server`."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
OPA = shutil.which("opa") or "/opt/homebrew/bin/opa"
BATCH = """package batch
results := [v | some c in input.cases; v := data.agentstack.authority.verdict with input as c]
"""


def verdicts(facts_list: list[dict]) -> list[dict]:
    batch = HERE / "_batch.rego"
    batch.write_text(BATCH)
    try:
        out = subprocess.run(
            [
                OPA,
                "eval",
                "-f",
                "json",
                "-d",
                str(HERE / "authority.rego"),
                "-d",
                str(batch),
                "--stdin-input",
                "data.batch.results",
            ],
            input=json.dumps({"cases": facts_list}),
            capture_output=True,
            text=True,
            check=True,
        )
    finally:
        batch.unlink()
    return json.loads(out.stdout)["result"][0]["expressions"][0]["value"]


class Server:
    """An OPA sidecar, the way it would be deployed."""

    def __enter__(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.proc = subprocess.Popen(
            [
                OPA,
                "run",
                "--server",
                "--addr",
                f"127.0.0.1:{self.port}",
                str(HERE / "authority.rego"),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(100):
            try:
                self.ask({"now": 0})
                return self
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("opa did not start")

    def ask(self, facts: dict) -> dict:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/data/agentstack/authority/verdict",
            data=json.dumps({"input": facts}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as r:
            return json.load(r).get("result", {})

    def __exit__(self, *exc):
        self.proc.terminate()
        self.proc.wait()
