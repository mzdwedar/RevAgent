"""The TabPFN licence gate (criterion 18).

TabPFN-3.5's weights are open, under a non-commercial licence that has to be accepted
once per machine through a gated Hugging Face repo. Not a paywall - an acceptance step,
and a real constraint on shipping this commercially.

Only some checkpoints are gated. `tabpfn` 9.x maps v2.5, v2.6, v3, v3.5 and v3.5-fast
to licence acceptance; **v2 is ungated** and would need none of this. v3.5 is a
deliberate choice, and `CHECKPOINT` in `engine.py` is where it is made.

A run that discovers the licence halfway through - after it has parked on a human
approval, or after it has drafted a candidate - has wasted a person's attention on
work it could never finish. So the check happens at startup.

Two checks, and the difference between them matters:

* `check_token` reads the environment. It is instant and it proves almost nothing - a
  well-formed string is not an accepted licence.
* `preflight` actually loads the model. It is slow, it needs the network, and it is
  the only check that answers the real question.

Startup runs both, in that order, so the common failure (nobody set the variable) is
reported in milliseconds and the real one is still caught.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from agentstack.storage import secrets

TOKEN_VARIABLE = "TABPFN_TOKEN"

HOW_TO_GET_ONE = (
    "Register at https://ux.priorlabs.ai, accept the licence on the Licenses tab, copy "
    "the API key from https://ux.priorlabs.ai/account, then export TABPFN_TOKEN."
)

# Short enough to catch an empty or obviously truncated value, loose enough not to
# pretend it knows the issuer's format. This is a typo check, not a validity check.
MINIMUM_TOKEN_LENGTH = 16


ENV_FILE = Path(__file__).resolve().parents[3] / ".env"


def load_env(path: Path | None = None) -> None:
    """Read KEY=VALUE lines from the repo-root `.env` into the environment, without overriding.

    The token lives in `.env` (gitignored), found from the repo root and not from wherever
    the process started; this saves exporting it by hand. Whatever the shell already set
    wins, and a missing file is not an error: `check_token` says what is missing and how to
    get it.
    """
    path = path or ENV_FILE
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


class LicenceRefused(RuntimeError):
    """The TabPFN licence is missing or not usable. Refused at startup, by design."""


def check_token(env: Mapping[str, str] | None = None) -> str:
    """Cheap, local, and honest about what it does not know."""
    found = secrets.read(TOKEN_VARIABLE, env=env)
    if found is None:
        raise LicenceRefused(
            f"{TOKEN_VARIABLE} is not set, so churn scoring cannot run. {HOW_TO_GET_ONE}"
        )
    token = found.reveal()
    if len(token) < MINIMUM_TOKEN_LENGTH:
        raise LicenceRefused(
            f"{TOKEN_VARIABLE} is {len(token)} characters, which is too short to be a key. "
            "This is a typo check; only loading the model proves a token is accepted."
        )
    return token
