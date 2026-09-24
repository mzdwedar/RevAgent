"""Shared helpers for the Temporal spike. Throwaway - not production code."""

from __future__ import annotations

import shutil

from temporalio.testing import WorkflowEnvironment

CLI = shutil.which("temporal") or "/opt/homebrew/opt/temporal/bin/temporal"


async def local_env(**kw) -> WorkflowEnvironment:
    return await WorkflowEnvironment.start_local(dev_server_existing_path=CLI, **kw)
