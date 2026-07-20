from __future__ import annotations

import json
from dataclasses import dataclass

from translation_service.config import Settings
from translation_service.domain.models import GlossaryEntry, TranslationLanguage
from translation_service.errors import ModelCallError, TranslationError
from translation_service.language.catalog import display_name
from translation_service.model.gateway import BaseModelGateway, parse_json_object


@dataclass(frozen=True, slots=True)
class MarkedTranslationUnit:
    unit_id: str
    marked_text: str


class BaseModelTranslator:
    def __init__(self, settings: Settings, model: BaseModelGateway) -> None:
        self._settings = settings
        self._model = model

    async def translate_text(
        self,
        *,
        text: str,
        source: TranslationLanguage,
        target: TranslationLanguage,
        tenant_id: str,
        glossary: list[GlossaryEntry],
    ) -> str:
        chunks = _split_plain_text(text, self._settings.model_batch_max_chars)
        translations: list[str] = []
        for chunk in chunks:
            response = await self._model.complete(
                tenant_id=tenant_id,
                system_prompt=self._plain_system_prompt(source, target),
                user_content=json.dumps(
                    {
                        "text": chunk,
                        "glossary": [entry.model_dump() for entry in glossary],
                    },
                    ensure_ascii=False,
                ),
            )
            translated = response.strip()
            if not translated:
                raise ModelCallError("Translation model returned empty text")
            translations.append(translated)
        return "\n\n".join(translations)

    async def translate_marked_units(
        self,
        *,
        units: list[MarkedTranslationUnit],
        source: TranslationLanguage,
        target: TranslationLanguage,
        tenant_id: str,
        glossary: list[GlossaryEntry],
    ) -> dict[str, str]:
        translated: dict[str, str] = {}
        for batch in self._batches(units):
            response = await self._model.complete(
                tenant_id=tenant_id,
                system_prompt=(
                    f"Translate each unit from {display_name(source)} to "
                    f"{display_name(target)}. Preserve every marker of the form "
                    "⟦R0001⟧, ⟦/R0001⟧, ⟦P0001⟧, ⟦T0001⟧, ⟦B0001⟧ or "
                    "⟦O0001⟧, ⟦F0001⟧ exactly once and in its original order. Markers are not "
                    "natural language. Use the context of the whole unit; do not translate "
                    "run fragments independently. Return JSON only as "
                    "{\"units\":[{\"id\":\"...\",\"text\":\"...\"}]}"
                ),
                user_content=json.dumps(
                    {
                        "units": [
                            {"id": unit.unit_id, "text": unit.marked_text}
                            for unit in batch
                        ],
                        "glossary": [entry.model_dump() for entry in glossary],
                    },
                    ensure_ascii=False,
                ),
            )
            payload = parse_json_object(response)
            rows = payload.get("units")
            if not isinstance(rows, list):
                raise ModelCallError("Translation model omitted the units array")
            expected_ids = {unit.unit_id for unit in batch}
            batch_result: dict[str, str] = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                unit_id = str(row.get("id") or "")
                text = row.get("text")
                if unit_id in expected_ids and isinstance(text, str) and text:
                    if unit_id in batch_result:
                        raise ModelCallError(
                            "Translation model returned a duplicate unit identifier"
                        )
                    batch_result[unit_id] = text
            if set(batch_result) != expected_ids:
                raise ModelCallError(
                    "Translation model did not return every requested unit"
                )
            translated.update(batch_result)
        return translated

    def _batches(
        self, units: list[MarkedTranslationUnit]
    ) -> list[list[MarkedTranslationUnit]]:
        batches: list[list[MarkedTranslationUnit]] = []
        current: list[MarkedTranslationUnit] = []
        current_chars = 0
        for unit in units:
            unit_chars = len(unit.marked_text)
            if unit_chars > self._settings.model_batch_max_chars:
                raise TranslationError(
                    "TRANSLATION_UNIT_TOO_LARGE",
                    "A paragraph or table cell exceeds the configured logical unit limit",
                )
            if current and (
                len(current) >= self._settings.model_batch_max_units
                or current_chars + unit_chars > self._settings.model_batch_max_chars
            ):
                batches.append(current)
                current = []
                current_chars = 0
            current.append(unit)
            current_chars += unit_chars
        if current:
            batches.append(current)
        return batches

    @staticmethod
    def _plain_system_prompt(
        source: TranslationLanguage, target: TranslationLanguage
    ) -> str:
        return (
            f"Translate from {display_name(source)} to {display_name(target)}. "
            "Preserve meaning, terminology, paragraph breaks, numbers and proper nouns. "
            "Follow the supplied glossary. Return only the translation, with no notes."
        )


def _split_plain_text(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(
                paragraph[index : index + max_chars]
                for index in range(0, len(paragraph), max_chars)
            )
            continue
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) > max_chars:
            chunks.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
