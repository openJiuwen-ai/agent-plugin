"""Record two OpenJiuwen ReActAgents, export one trajectory, and print how to evaluate it.

Usage (any OpenAI-compatible endpoint; OpenRouter shown):

    export AGENT_API_KEY=...                         # key for the agents' model
    export AGENT_API_BASE=https://openrouter.ai/api/v1
    export AGENT_MODEL=qwen/qwen3-32b
    python quickstart.py

The only plugin-specific lines are marked with  # <-- plugin
"""

import asyncio
import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import certifi
from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig, ProviderType
from openjiuwen.core.foundation.tool import tool
from openjiuwen.core.single_agent.agents.react_agent import ReActAgent, ReActAgentConfig
from openjiuwen.core.single_agent.schema.agent_card import AgentCard

from ojw_observability import ObservabilityRail, merge_workflow  # <-- plugin

# OpenJiuwen requires SAFE_CERT_DIR when TLS verification is enabled.
CERT_FILE = os.path.realpath(certifi.where())
os.environ.setdefault("SAFE_CERT_DIR", os.path.dirname(CERT_FILE))


async def word_count(text: str) -> int:
    """Count the words in a text."""
    return len(text.split())


def build_agent(name: str, system_prompt: str, tools=()) -> ReActAgent:
    """An ordinary OpenJiuwen ReActAgent; nothing here is plugin-specific."""
    agent = ReActAgent(AgentCard(id=f"{name}_{uuid4().hex[:8]}", name=name,
                                 description=system_prompt))
    agent.configure(ReActAgentConfig(
        model_client_config=ModelClientConfig(
            client_provider=ProviderType.OpenAI,  # any OpenAI-compatible endpoint
            api_key=os.environ["AGENT_API_KEY"],
            api_base=os.environ["AGENT_API_BASE"],
            verify_ssl=True, ssl_cert=CERT_FILE, timeout=300,
        ),
        model_config_obj=ModelRequestConfig(model=os.environ["AGENT_MODEL"], temperature=0.0),
        prompt_template=[{"role": "system", "content": system_prompt}],
        max_iterations=5,
    ))
    for item in tools:
        agent.ability_manager.add_ability(item.card, item)
    return agent


async def run_observed(agent: ReActAgent, query: str, run_dir: Path) -> str:
    """Invoke one agent with an observation Rail attached."""
    rail = ObservabilityRail().configure(output_dir=run_dir)  # <-- plugin
    await agent.register_rail(rail)                           # <-- plugin
    try:
        response = await agent.invoke({"query": query, "conversation_id": uuid4().hex})
        return response.get("output", "")
    finally:
        await agent.unregister_rail(rail)                     # <-- plugin


async def main() -> None:
    # 1. One fresh directory per workflow run.
    run_dir = Path("runs") / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_dir.mkdir(parents=True)
    run_dir = run_dir.resolve()

    # 2. Your workflow: two agents, one after the other.
    writer = build_agent("Writer", "Write a three-sentence summary of the topic. "
                                   "Use the word_count tool to check its length.",
                         tools=[tool(word_count)])
    reviewer = build_agent("Reviewer", "Review the summary and list up to three concrete improvements.")

    summary = await run_observed(writer, "Topic: why agents need observability.", run_dir)
    review = await run_observed(reviewer, f"Review this summary:\n{summary}", run_dir)
    print(review)

    # 3. Export the completed run as one trajectory.
    trajectory = merge_workflow(                               # <-- plugin
        sorted(run_dir.glob("agent_*.json")),
        run_dir / "agentic_trajectory.json",
        scenario_meta={"uc": "Quickstart", "scenario": "WriteAndReview",
                       "system_architecture": "sequential"},
    )

    # 4. Evaluate it (see "Evaluate the trajectory" in the guide).
    print(f"\nTrajectory saved: {trajectory}")
    print("Evaluate it with, for example:\n"
          "  ojw_evaluation run failure-detector --strategy taxonomy-guided --taxonomy ours \\\n"
          f"    --client openrouter --model-id <judge-model> --trajectory {trajectory}")


if __name__ == "__main__":
    asyncio.run(main())
