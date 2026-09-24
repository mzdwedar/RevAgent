"""A workflow module that imports the gateway. Does the sandbox notice?"""

from temporalio import workflow

from agentstack.execution.gateway import Gateway


@workflow.defn
class Leaky:
    @workflow.run
    async def run(self) -> str:
        # Touching the gateway class from workflow code - still no sandbox complaint.
        return "ran" if Gateway.execute else "unreachable"
