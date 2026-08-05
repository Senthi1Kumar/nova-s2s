"""Sanitize Llama special-token storms.

Callers: pytest. Exercises nova_hailo.backends.llm.sanitize_*.
No data files.
"""
from __future__ import annotations

from nova_hailo.backends.llm import sanitize_llm_text, sanitize_llm_token


def test_sanitize_drops_header_loop():
    junk = (
        "1 assistant<|start header id| <|start header id| <|start header id| "
        "<|start header id|"
    )
    assert sanitize_llm_text(junk) == ""


def test_sanitize_keeps_normal_sentence():
    assert "Nova" in sanitize_llm_text("I am Nova, your cabin assistant.")


def test_sanitize_token_strips_control():
    assert sanitize_llm_token("<|eot_id|>") == ""
    assert "Hi" in sanitize_llm_token("Hi")
