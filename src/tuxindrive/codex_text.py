"""Bounded projection of Codex journals into readable conversation text.

Only explicit user/assistant message text is exported. Tool outputs, images,
attachments, reasoning and context records are never decoded into the archive.
The source is consumed directly: no full-size raw staging copy is needed.
"""
from __future__ import annotations

import hashlib
import json
import re


MAX_RECORD_BYTES = 64 * 1024 * 1024


class ConversationTextError(ValueError):
    pass


class ConversationSourceChanged(RuntimeError):
    pass


def _visible_text(message):
    if not isinstance(message, dict) or message.get("type") != "message":
        return ""
    role = message.get("role")
    if role not in ("user", "assistant"):
        return ""
    if message.get("channel") not in (None, "final", "commentary"):
        return ""
    if role == "assistant" and message.get("phase") not in (None, "final", "commentary"):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    accepted = ("input_text", "text") if role == "user" else ("output_text", "text")
    parts = [part["text"] for part in content if isinstance(part, dict)
             and part.get("type") in accepted and isinstance(part.get("text"), str)]
    text = "\n\n".join(parts)
    # These blocks are UI/file context, not typed conversation text.
    text = re.sub(r"<(in-app-browser-context|environment_context|user_instructions|image|audio)\b[^>]*>.*?</\1>",
                  "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"!\[[^\]\n]*\]\(\s*data:[^)]+\)", "[embedded image omitted]", text,
                  flags=re.IGNORECASE)
    text = re.sub(r"data:(?:image|audio|video|application)/[^\s]+", "[embedded file omitted]", text,
                  flags=re.IGNORECASE)
    return text.strip()


def export_conversation(source, captured_size, write, *, cancelled=None, progress=None, _events_only=False):
    """Read exactly the initial prefix; return digest, messages and deferred tail.

    The caller verifies prefix stability and source identity before accepting
    the disposable output. `write` performs private, free-space-checked writes.
    Event-message echoes are deliberately not exported twice.
    """
    remaining, messages, records = captured_size, 0, 0
    digest, deferred, legacy_events = hashlib.sha256(), False, False
    while remaining:
        if cancelled and cancelled():
            raise InterruptedError("Conversation text export cancelled")
        line = source.readline(min(MAX_RECORD_BYTES + 1, remaining))
        if not line:
            raise ConversationSourceChanged("A Codex conversation was truncated during text export")
        remaining -= len(line)
        digest.update(line)
        if len(line) > MAX_RECORD_BYTES:
            raise ConversationTextError("A Codex record exceeds the bounded text export limit")
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError):
            if remaining == 0 and not line.endswith(b"\n") and records:
                deferred = True
                break
            raise ConversationTextError("A completed Codex conversation record is invalid") from None
        records += 1
        if not isinstance(record, dict):
            raise ConversationTextError("A Codex conversation record is not an object")
        payload = record.get("payload")
        if record.get("type") == "event_msg" and isinstance(payload, dict):
            if payload.get("type") in ("user_message", "agent_message") and isinstance(payload.get("message"), str):
                legacy_events = True
                if _events_only:
                    role = "user" if payload["type"] == "user_message" else "assistant"
                    payload = {"type": "message", "role": role, "content": [{
                        "type": "input_text" if role == "user" else "output_text", "text": payload["message"]}]}
        if (not _events_only and record.get("type") == "response_item"
                or _events_only and record.get("type") == "event_msg"):
            message = payload
            text = _visible_text(message)
            if text:
                timestamp = record.get("timestamp", "")
                timestamp = timestamp if isinstance(timestamp, str) else ""
                timestamp = timestamp.replace("\n", " ").replace("\r", " ")[:80]
                role = "User" if message["role"] == "user" else "Assistant"
                write(f"## {role}" + (f" — {timestamp}" if timestamp else "") + "\n\n" + text + "\n\n")
                messages += 1
        if progress:
            progress(captured_size - remaining)
    if not messages and legacy_events and not _events_only:
        # Older journals may only contain display events. Use them only when
        # there are no canonical messages; never duplicate their modern echoes.
        source.seek(0)
        return export_conversation(source, captured_size, write, cancelled=cancelled,
                                   progress=progress, _events_only=True)
    return digest.digest(), messages, deferred
