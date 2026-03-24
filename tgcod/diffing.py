from __future__ import annotations

from difflib import SequenceMatcher


def normalize_window_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip("\n")


def extract_increment(previous: str, current: str) -> str:
    previous_norm = normalize_window_text(previous)
    current_norm = normalize_window_text(current)

    if current_norm == previous_norm:
        return ""
    if not previous_norm:
        return current_norm
    if current_norm.startswith(previous_norm) and (
        len(current_norm) == len(previous_norm) or current_norm[len(previous_norm)] == "\n"
    ):
        return current_norm[len(previous_norm) :].lstrip("\n")

    previous_lines = previous_norm.splitlines()
    current_lines = current_norm.splitlines()
    matcher = SequenceMatcher(a=previous_lines, b=current_lines)
    chunks: list[str] = []

    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in {"insert", "replace"}:
            block = "\n".join(current_lines[j1:j2]).strip()
            if block:
                chunks.append(block)

    deduped: list[str] = []
    for chunk in chunks:
        if chunk not in deduped:
            deduped.append(chunk)
    return "\n".join(deduped).strip()


def chunk_message(text: str, limit: int) -> list[str]:
    normalized = normalize_window_text(text)
    if not normalized:
        return []
    if len(normalized) <= limit:
        return [normalized]

    pieces: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in normalized.splitlines():
        extra = len(line) + (1 if current else 0)
        if current and current_len + extra > limit:
            pieces.append("\n".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += extra
    if current:
        pieces.append("\n".join(current))
    return pieces
