"""Plugin entry point for opening the current dialogue's evaluation UI."""
from openjiuwen.core.foundation.tool import Tool, ToolCard

from ..rails.evaluation.opening import open_current_dialogue


class OpenDialogueEvaluationTool(Tool):
    def __init__(self) -> None:
        super().__init__(ToolCard(
            id='open_dialogue_evaluation', name='open_dialogue_evaluation',
            description='When the user asks to open evaluation for this dialogue, return its local UI link. '
                        'Uses the actual session and all recorded turns. Does not run evaluation; '
                        'the user chooses credentials and presses Run Evaluation in the UI.',
            input_params={'type': 'object', 'properties': {}},
        ))

    async def invoke(self, inputs, **kwargs):
        return await open_current_dialogue(kwargs.get('session'))

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)
