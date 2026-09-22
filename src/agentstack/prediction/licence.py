"""The TabPFN licence gate (criterion 18).

TabPFN's weights sit behind a licence gate at PriorLabs, fetched from Hugging Face on
first use. A run that discovers this halfway through - after it has parked on a human
approval, or after it has drafted a candidate - has wasted a person's attention on work
it could never finish. So the check happens at startup.

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

TOKEN_VARIABLE = "TABPFN_TOKEN"

HOW_TO_GET_ONE = (
    "Register at https://ux.priorlabs.ai, accept the licence on the Licenses tab, copy "
    "the API key from https://ux.priorlabs.ai/account, then export TABPFN_TOKEN."
)

# Short enough to catch an empty or obviously truncated value, loose enough not to
# pretend it knows the issuer's format. This is a typo check, not a validity check.
MINIMUM_TOKEN_LENGTH = 16


class LicenceRefused(RuntimeError):
    """The TabPFN licence is missing or not usable. Refused at startup, by design."""


def check_token(env: Mapping[str, str] | None = None) -> str:
    """Cheap, local, and honest about what it does not know."""
    source = os.environ if env is None else env
    token = source.get(TOKEN_VARIABLE, "").strip()
    if not token:
        raise LicenceRefused(
            f"{TOKEN_VARIABLE} is not set, so churn scoring cannot run. {HOW_TO_GET_ONE}"
        )
    if len(token) < MINIMUM_TOKEN_LENGTH:
        raise LicenceRefused(
            f"{TOKEN_VARIABLE} is {len(token)} characters, which is too short to be a key. "
            "This is a typo check; only loading the model proves a token is accepted."
        )
    return token
