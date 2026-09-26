from __future__ import annotations

import re


_RECORD_START = re.compile(
    r"^(?:\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}"
    r"|\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})"
)


def newest_first_log(content: str) -> str:
    """Render recent log records newest-first without splitting continuations."""
    lines = content.strip().splitlines()
    if not lines:
        return ""

    records: list[list[str]] = []
    current: list[str] = []
    timestamped = False
    for line in lines:
        if _RECORD_START.match(line):
            timestamped = True
            if current:
                records.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        records.append(current)

    # Provider logs without timestamps are still useful. Treat each line as a
    # record so newly appended output appears at the top consistently.
    if not timestamped:
        records = [[line] for line in lines]

    return "\n".join("\n".join(record) for record in reversed(records))
