"""Prompt templates with checked placeholder substitution."""
from __future__ import annotations

import string


class PromptTemplate:
    """A system/user prompt pair whose placeholders are validated at render time."""

    def __init__(self, system: str, user: str, variables: list[str] | None = None):
        self.system = system
        self.user = user
        self.variables = set(variables or self.extract_placeholders(system + user))

    @staticmethod
    def extract_placeholders(text: str) -> list[str]:
        return [name for _, name, _, _ in string.Formatter().parse(text) if name]

    def render(self, **kwargs) -> dict[str, str]:
        missing = self.variables - kwargs.keys()
        if missing:
            raise ValueError(f"Missing values for placeholders: {sorted(missing)}")
        return {
            "system": self.system.format(**kwargs),
            "user": self.user.format(**kwargs),
        }
