"""Script generation: story text becomes narratable, numbered parts."""

from pulpmill.scripting.provider import ScriptBrief, ScriptGuidance, ScriptProvider
from pulpmill.scripting.service import ScriptBuilder, ScriptResult, build_script_provider
from pulpmill.scripting.speech import to_speech_text

__all__ = [
    "ScriptBrief",
    "ScriptBuilder",
    "ScriptGuidance",
    "ScriptProvider",
    "ScriptResult",
    "build_script_provider",
    "to_speech_text",
]
