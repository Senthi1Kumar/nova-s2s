"""Base abstraction for Nova tools.

A ``NovaTool`` is a single, atomically-named, well-described callable the model
can invoke via native OpenAI-style tool calling. ``to_function_tool()`` renders
the shape s2s (and the underlying OpenAI Realtime API types) expects for a
function tool: ``{"type": "function", "name", "description", "parameters"}``
(see ``openai.types.realtime.RealtimeFunctionTool`` /
``speech_to_speech.LLM.tool_call.function_tool.FunctionTool``, confirmed in
Task 2's findings). ``execute()`` runs the tool for real against whatever
backing store it owns and returns a JSON-able result dict.

Kept deliberately generic so Task 4 (external/MCP tools) and Task 5 (tool
service/registry) can subclass or hold this without changes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class NovaTool(ABC):
    """A single atomic, model-callable tool.

    Subclasses set ``name``, ``description``, and ``parameters`` (a JSON-schema
    object describing the tool's arguments) as class or instance attributes,
    and implement ``execute()``.
    """

    name: str
    description: str
    parameters: dict[str, Any]

    def to_function_tool(self) -> dict[str, Any]:
        """Render this tool as an s2s-compatible FunctionTool dict."""
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    @abstractmethod
    def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Run the tool for real and return a JSON-able result dict."""
        raise NotImplementedError
