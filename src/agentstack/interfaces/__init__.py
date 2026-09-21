"""Layer 1 - interfaces and channels.

Where work enters. A channel carries an event; it does not resolve identity, apply
policy or assemble state. Nothing else in the stack imports this package - that is
`.importlinter` contract 4, and it is what keeps the entry point swappable.
"""

from agentstack.interfaces.inbound import InboundEvent

__all__ = ["InboundEvent"]
