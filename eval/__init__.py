"""Nova s2s-native eval corpus (mocked / lexical route — never LiteRT).

Corpus version tracks fixture schema + gold-label conventions. Bump when
``fixtures/s2s_turns.jsonl`` fields or auth_status vocabulary change.
"""

CORPUS_VERSION = 1

# DriveAuth gold labels (fixture shorthand → live bridge status).
AUTH_STATUS_ALIASES: dict[str, str] = {
    "accept": "accept",
    "step_up": "step_up_required",
    "step_up_required": "step_up_required",
    "reject": "denied",
    "denied": "denied",
    "bypass": "bypass",
}
