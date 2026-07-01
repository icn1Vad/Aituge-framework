from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class OutputParseResult:
    structured: Any
    normalized_text: str
    error: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def parse_json_output(content: str) -> OutputParseResult:
    text = _strip_markdown_fence((content or "").strip())
    candidates = [text]
    extracted = _extract_first_json_value(text)
    if extracted and extracted not in candidates:
        candidates.append(extracted)

    for candidate in candidates:
        for repaired in _repair_candidates(candidate):
            try:
                return OutputParseResult(
                    structured=json.loads(repaired),
                    normalized_text=repaired,
                )
            except json.JSONDecodeError:
                continue

    return OutputParseResult(
        structured=None,
        normalized_text=text,
        error={
            "type": "JSONDecodeError",
            "message": "Unable to parse model output as JSON.",
            "preview": text[:500],
        },
    )


def _strip_markdown_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _extract_first_json_value(text: str) -> str | None:
    starts = [(text.find("{"), "{", "}"), (text.find("["), "[", "]")]
    starts = [item for item in starts if item[0] >= 0]
    if not starts:
        return None
    start, open_char, close_char = min(starts, key=lambda item: item[0])
    depth = 0
    in_string = False
    escape = False

    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return text[start:index + 1].strip()
    return None


def _repair_candidates(text: str) -> list[str]:
    normalized = text.strip().replace("\ufeff", "")
    no_trailing_commas = re.sub(r",(\s*[}\]])", r"\1", normalized)
    escaped_inner_quotes = _escape_unescaped_inner_quotes(no_trailing_commas)
    values = [normalized]
    if no_trailing_commas != normalized:
        values.append(no_trailing_commas)
    if escaped_inner_quotes not in values:
        values.append(escaped_inner_quotes)
    return values


def _escape_unescaped_inner_quotes(text: str) -> str:
    """Repair common model JSON mistakes like: "军医"5+3"一体化".

    A quote inside a JSON string is only treated as closing when the next
    non-space character can legally follow a JSON string boundary.
    """

    chars: list[str] = []
    in_string = False
    escape = False
    length = len(text)

    for index, char in enumerate(text):
        if not in_string:
            chars.append(char)
            if char == '"':
                in_string = True
            continue

        if escape:
            chars.append(char)
            escape = False
            continue

        if char == "\\":
            chars.append(char)
            escape = True
            continue

        if char == '"':
            next_index = index + 1
            while next_index < length and text[next_index].isspace():
                next_index += 1
            next_char = text[next_index] if next_index < length else ""
            if next_char in {",", "}", "]", ":"} or not next_char:
                chars.append(char)
                in_string = False
            else:
                chars.append('\\"')
            continue

        chars.append(char)

    return "".join(chars)
