"""The workflow-global step counter - ONE API (§3.2c resolved).

The hierarchical handler's per-invocation allocator is a different
concept and lives there as InvocationStepCounter.
"""
from dataclasses import dataclass


# Small state holder used to assign readable sequential step IDs across all agents.
@dataclass
class StepCounter:
    """
   Workflow-global counters shared by ALL agents in one run.

   agent_ordinal : how many agents have started (1, 2, 3, ...) — used for agent_order_step_id and the agent_<n> identifier.
   step : monotonically increasing step number across the whole workflow, so step IDs never restart between agents.
   """
    agent_ordinal: int = 0
    step: int = 0

    # Increment and return the next global workflow step number.
    def next_step(self) -> int:
        """Advance and return the next global workflow step number."""
        self.step += 1
        return self.step
