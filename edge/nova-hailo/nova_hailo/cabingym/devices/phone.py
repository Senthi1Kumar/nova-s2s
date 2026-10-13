# nova_hailo/cabingym/devices/phone.py
from __future__ import annotations

from typing import Any

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.devices.base import (
    Api, ApiContext, ApiResult, clarify, device, done, p_bool, p_entity, p_span, p_value,
)

_TRUNCATE_WORDS = 12


def _call(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    if ctx.snapshot[c.CALL_STATE] in ("dialing", "active"):
        return clarify("You're already on a call.")
    name = args["name"]
    return done(f"Calling {name}.", {c.CALL_STATE: "dialing", c.CALL_PEER: name, c.LAST_DIALED: name})


def _answer(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    if ctx.snapshot[c.CALL_STATE] != "ringing":
        return clarify("There's no incoming call.")
    return done(f"Connected to {ctx.snapshot[c.CALL_PEER]}.", {c.CALL_STATE: "active"})


def _reject(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    if ctx.snapshot[c.CALL_STATE] != "ringing":
        return clarify("There's no incoming call.")
    return done("Call declined.", {c.CALL_STATE: "idle", c.CALL_PEER: ""})


def _end(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    if ctx.snapshot[c.CALL_STATE] not in ("dialing", "active"):
        return clarify("There's no call to end.")
    return done("Call ended.", {c.CALL_STATE: "idle", c.CALL_PEER: "", c.CALL_MUTED: False})


def _mute(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    if ctx.snapshot[c.CALL_STATE] != "active":
        return clarify("You're not on a call.")
    on = bool(args.get("on", True))
    return done("Muted." if on else "Unmuted.", {c.CALL_MUTED: on})


def _read(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    read = tuple(ctx.snapshot[c.READ_IDS])
    unread = [m for m in ctx.snapshot[c.MESSAGES] if m["id"] not in read]
    sender = args.get("sender")
    if sender:
        unread = [m for m in unread if m["from"] == sender]
    unread.sort(key=lambda m: m["ts"], reverse=True)
    picked = unread[: int(args.get("count") or 1)]
    if not picked:
        return done(f"You have no new messages{' from ' + sender if sender else ''}.")
    parts = []
    for m in picked:
        text = m["text"]
        if args.get("_truncate"):
            words = text.split()
            if len(words) > _TRUNCATE_WORDS:
                text = " ".join(words[:_TRUNCATE_WORDS]) + "…"
        parts.append(f"{m['from']} says: {text}")
    return done(" Then, ".join(parts), {c.READ_IDS: read + tuple(m["id"] for m in picked)},
                data=picked)


def _send(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    msg = {"to": args["recipient"], "text": args["text"]}
    return done(f"Sent to {args['recipient']}.", {c.OUTBOX: tuple(ctx.snapshot[c.OUTBOX]) + (msg,)})


def _missed(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    names = [m["from"] for m in ctx.snapshot[c.MISSED]]
    if not names:
        return done("No missed calls.")
    if len(names) == 1:
        return done(f"You missed a call from {names[0]}.")
    return done(f"You missed calls from {', '.join(names[:-1])} and {names[-1]}.")


def _redial(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    last = ctx.snapshot[c.LAST_DIALED]
    if not last:
        return clarify("I don't have anyone to redial.")
    return _call(ctx, {"name": last})


_CALL_WRITES = (c.CALL_STATE, c.CALL_PEER, c.LAST_DIALED)
DEVICE = device(
    "phone", "phone calls and text messages",
    r"\b(call\w*|dial|phone|ring\w*|text\w*|messages?|sms|answer|decline|reject|hang|mute|"
    r"voicemail|missed|redial)\b",
    [
        Api("read_messages", "read new text messages", _read,
            params={"sender": p_entity("senders", "Only messages from this person"),
                    "count": p_value("count", "How many messages", 1, 5)},
            writes=(c.READ_IDS,),
            patterns=(r"\b(read|check|any|what('?s|\s+are|\s+is)|do\s+i\s+have|new)\b.*"
                      r"\b(messages?|texts?|sms|notifications)\b",)),
        Api("send_message", "send a text message to a contact", _send,
            params={"recipient": p_entity("contacts", "Who to text"),
                    "text": p_span("message", "Message text")},
            required=("recipient", "text"), writes=(c.OUTBOX,),
            patterns=(r"\b(text|message|send\s+(a\s+)?(text|message|sms)|tell)\b(?!\s*(me|us)\b)",)),
        Api("answer_call",
            "answer a phone call that is ringing right now (picking someone up at a place "
            "is a navigation stop, not a call)", _answer, writes=(c.CALL_STATE,),
            # "pick up my child at school" is a stop: only call words or a bare reply.
            patterns=(r"^\s*(please\s+)?(answer|accept|pick\s+(it\s+)?up)(\s+(it|that))?"
                      r"(\s+please)?\s*[.!]*$",
                      r"\b(answer|accept|take)\s+(the\s+|that\s+|this\s+)?(call|phone)\b",
                      r"\bpick\s+up\s+(the\s+)?(call|phone)\b|\bpick\s+(the\s+)?(call|phone)\s+up\b")),
        Api("reject_call", "decline the incoming call", _reject, writes=(c.CALL_STATE, c.CALL_PEER),
            patterns=(r"\b(reject|decline|ignore|dismiss)\b|\bsend\s+(it\s+)?to\s+voicemail\b",)),
        Api("end_call", "hang up the current call", _end,
            writes=(c.CALL_STATE, c.CALL_PEER, c.CALL_MUTED),
            patterns=(r"\b(hang\s+up|end\s+(the\s+)?call|stop\s+(the\s+)?call|disconnect)\b",)),
        Api("mute_call", "mute or unmute the microphone on a call", _mute,
            params={"on": p_bool("Mute on", parser="mute", default=True)}, writes=(c.CALL_MUTED,),
            patterns=(r"\b(un)?mute\s+(the\s+)?(call|mic|microphone|myself|me)\b",)),
        Api("list_missed_calls", "list missed calls", _missed, read_only=True,
            patterns=(r"\bmissed\s+calls?\b|\bwho\s+(called|rang)\b",)),
        Api("redial", "call the last number again", _redial, writes=_CALL_WRITES,
            patterns=(r"\bredial\b|\bcall\s+(them\s+)?(back|again)\b",)),
        Api("call_contact", "call a contact", _call,
            params={"name": p_entity("contacts", "Who to call")}, required=("name",),
            writes=_CALL_WRITES, patterns=(r"\b(call|dial|phone|ring)\b",)),
    ],
)
