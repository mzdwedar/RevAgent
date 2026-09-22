"""Prove the service can do its job before it accepts work (criterion 18).

    uv run agentstack-preflight

A run that parks on a human approval and only then discovers it cannot score has spent
someone's attention on work it could never finish. This is what the service entry point
calls first, and what a deploy should call before shifting traffic.
"""

from __future__ import annotations

import argparse
import sys

from agentstack.prediction.churn import ScoringError
from agentstack.prediction.engine import TabPFNScorer
from agentstack.prediction.licence import LicenceRefused, check_token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Startup checks for the experiment operator.")
    parser.add_argument(
        "--token-only",
        action="store_true",
        help="check the environment variable and stop; does not load the weights",
    )
    args = parser.parse_args(argv)

    try:
        check_token()
        print("ok    TABPFN_TOKEN is set and plausibly shaped")
        if args.token_only:
            print("\nnot checked: whether the licence is accepted. Only loading the model")
            print("answers that, and --token-only skips it deliberately.")
            return 0
        version = TabPFNScorer().preflight()
        print(f"ok    {version} loaded its weights; scoring is available")
    except (LicenceRefused, ScoringError) as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
