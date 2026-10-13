# nova_hailo/cabingym/confirm.py
"""Yes/no replies to a pending vehicle confirmation. Callers: OemToolGateway, CabinGymEnv."""
from __future__ import annotations

import re

_YES = re.compile(
    r"^\s*(yes|yeah|yep|yup|sure|ok(ay)?|confirm(ed)?|do\s+it|go\s+ahead|please\s+do|send\s+it|"
    r"affirmative|correct|absolutely|of\s+course|sounds\s+good|go\s+for\s+it|let'?s\s+do\s+it|"
    r"that'?s\s+right)\b"
    # "Switch it." / "Change that." -- the question's own verb, pointing back at it.
    r"|^\s*(please\s+)?(switch|change|do|apply|set|turn)\s+(it|that)(\s+(on|off|please))?\s*[.!]*$",
    re.I)
_NO = re.compile(
    r"^\s*(no|nope|nah|cancel|don'?t|do\s+not|never\s*mind|forget\s+it|negative|not\s+now|"
    r"leave\s+it|keep\s+it)\b|^\s*stop\s*[.!]*$",
    re.I)


def parse_confirmation(text: str) -> bool | None:
    if _YES.search(text or ""):
        return True
    if _NO.search(text or ""):
        return False
    return None
