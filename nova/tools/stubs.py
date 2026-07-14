"""Believable canned responses for MCP integrations not yet wired up.

Gmail/Calendar/Spotify OAuth is out of scope for this milestone (see
CLAUDE.md's M4 "real MCP tools" milestone); these stubs let a demo show the
capability and let the model practice calling the right tool for the right
intent now, ahead of real OAuth wiring. Each ``execute()`` below is a
simulated/fixture response (NOT a live account) — this is a note for future
maintainers replacing these with real MCP calls, not something the model
should ever say aloud.
"""
from __future__ import annotations

from typing import Any

from nova.tools.base import NovaTool


class GmailStubTool(NovaTool):
    name = "check_email"
    description = "Check the user's email inbox for unread messages."
    parameters = {"type": "object", "properties": {}, "required": []}

    def execute(self) -> dict[str, Any]:
        # Simulated inbox snapshot — replace with a real Gmail MCP call in M4.
        return {
            "status": "success",
            "unread_count": 3,
            "messages": [
                {"from": "Priya Sharma", "subject": "Re: Q3 roadmap review", "preview": "Sounds good, let's sync tomorrow."},
                {"from": "GitHub", "subject": "[nova_v3] New PR requires your review", "preview": "1 pull request is waiting on your review."},
                {"from": "Aditya Rao", "subject": "Dinner Friday?", "preview": "We're thinking 7pm at the usual place."},
            ],
        }


class CalendarStubTool(NovaTool):
    name = "check_calendar"
    description = "Check the user's personal calendar for upcoming events."
    parameters = {"type": "object", "properties": {}, "required": []}

    def execute(self) -> dict[str, Any]:
        # Simulated calendar snapshot — replace with a real Calendar MCP call in M4.
        return {
            "status": "success",
            "events": [
                {"title": "1:1 with manager", "start": "today 15:00"},
                {"title": "Dentist appointment", "start": "tomorrow 10:30"},
            ],
        }


class SpotifyStubTool(NovaTool):
    name = "play_music"
    description = "Play music via Spotify, optionally naming a song, artist, or playlist."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Song, artist, or playlist to play. Optional."},
        },
        "required": [],
    }

    def execute(self, query: str = "") -> dict[str, Any]:
        # Simulated playback state — replace with a real Spotify MCP call in M4.
        now_playing = query or "your Daily Mix 1"
        return {"status": "success", "now_playing": now_playing, "device": "car speakers"}


class SendEmailStubTool(NovaTool):
    # SIMULATED: no real email is sent (no OAuth in this milestone). Exists to
    # exercise the ConfirmationGate two-step flow on a genuinely irreversible-
    # shaped action; swap in a real Gmail MCP call in a later milestone.
    name = "send_email"
    description = (
        "Send an email on the driver's behalf. Irreversible: always ask the driver to approve "
        "the recipient, subject, and content before sending."
    )
    parameters = {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Recipient email address."},
            "subject": {"type": "string", "description": "Subject line."},
            "body": {"type": "string", "description": "Email body text."},
        },
        "required": ["to", "subject", "body"],
    }

    def execute(self, to: str, subject: str, body: str) -> dict[str, Any]:
        return {"status": "sent", "to": to, "subject": subject, "message_id": "sim-000123"}
