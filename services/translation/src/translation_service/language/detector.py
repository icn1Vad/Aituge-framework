from __future__ import annotations

import json
from dataclasses import dataclass

from translation_service.config import Settings
from translation_service.domain.models import TranslationLanguage
from translation_service.errors import ModelCallError, TranslationError
from translation_service.model.gateway import BaseModelGateway, parse_json_object


@dataclass(frozen=True, slots=True)
class DetectionResult:
    language: TranslationLanguage
    confidence: float


class LanguageDetector:
    def __init__(self, settings: Settings, model: BaseModelGateway) -> None:
        self._settings = settings
        self._model = model

    async def resolve(
        self,
        *,
        requested: TranslationLanguage,
        target: TranslationLanguage,
        sample: str,
        tenant_id: str,
    ) -> DetectionResult:
        if requested is not TranslationLanguage.AUTO:
            if requested is target:
                raise TranslationError(
                    "SOURCE_EQUALS_TARGET",
                    "Source and target languages must be different",
                )
            return DetectionResult(requested, 1.0)

        normalized = " ".join(sample.split())
        if len(normalized) < self._settings.detection_min_chars:
            raise TranslationError(
                "SOURCE_LANGUAGE_UNDETERMINED",
                "Not enough extractable text to detect the source language; specify it explicitly",
            )
        supported = [
            language.value
            for language in TranslationLanguage
            if language is not TranslationLanguage.AUTO
        ]
        response = await self._model.complete(
            tenant_id=tenant_id,
            system_prompt=(
                "Detect the dominant natural language of the supplied sample. "
                "Return JSON only with keys language and confidence. language must be "
                f"one of {supported}; confidence must be from 0 to 1. Do not translate."
            ),
            user_content=json.dumps(
                {"sample": normalized[: self._settings.detection_sample_chars]},
                ensure_ascii=False,
            ),
            max_tokens=128,
            temperature=0,
        )
        try:
            payload = parse_json_object(response)
        except ModelCallError as exc:
            raise TranslationError(
                "SOURCE_LANGUAGE_UNDETERMINED",
                "The model did not return a valid language detection result",
            ) from exc
        try:
            language = TranslationLanguage(str(payload.get("language", "")))
            confidence = float(payload.get("confidence"))
        except (TypeError, ValueError) as exc:
            raise TranslationError(
                "SOURCE_LANGUAGE_UNDETERMINED",
                "The model did not return a supported source language",
            ) from exc
        if (
            language is TranslationLanguage.AUTO
            or not 0 <= confidence <= 1
            or confidence < self._settings.detection_min_confidence
        ):
            raise TranslationError(
                "SOURCE_LANGUAGE_UNDETERMINED",
                "Source language confidence is below the configured threshold",
            )
        if language is target:
            raise TranslationError(
                "SOURCE_EQUALS_TARGET",
                "Detected source language is the same as the target language",
            )
        return DetectionResult(language, confidence)
