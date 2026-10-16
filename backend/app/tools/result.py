"""Keep a bounded display separate from the complete returned business result."""
from __future__ import annotations


class ToolResultText(str):
    def __new__(cls, preview: str, *, full_content: str):
        value = str.__new__(cls, preview)
        value.full_content = full_content
        return value
