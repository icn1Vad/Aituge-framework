from __future__ import annotations

from dataclasses import dataclass

from translation_service.domain.models import TranslationLanguage
from translation_service.errors import TranslationError


@dataclass(frozen=True, slots=True)
class LanguageMapping:
    display_name: str
    babeldoc_code: str


LANGUAGE_MAPPINGS: dict[TranslationLanguage, LanguageMapping] = {
    TranslationLanguage.ZH_CN: LanguageMapping("Simplified Chinese", "zh-CN"),
    TranslationLanguage.ZH_TW: LanguageMapping("Traditional Chinese", "zh-TW"),
    TranslationLanguage.EN: LanguageMapping("English", "en"),
    TranslationLanguage.JA: LanguageMapping("Japanese", "ja"),
    TranslationLanguage.KO: LanguageMapping("Korean", "ko"),
    TranslationLanguage.FR: LanguageMapping("French", "fr"),
    TranslationLanguage.DE: LanguageMapping("German", "de"),
    TranslationLanguage.ES: LanguageMapping("Spanish", "es"),
    TranslationLanguage.RU: LanguageMapping("Russian", "ru"),
    TranslationLanguage.AR: LanguageMapping("Arabic", "ar"),
    TranslationLanguage.PT: LanguageMapping("Portuguese", "pt"),
}


def display_name(language: TranslationLanguage) -> str:
    return _mapping(language).display_name


def babeldoc_code(language: TranslationLanguage) -> str:
    return _mapping(language).babeldoc_code


def ensure_language_pair(
    source: TranslationLanguage, target: TranslationLanguage
) -> None:
    if target is TranslationLanguage.AUTO:
        raise TranslationError(
            "INVALID_TARGET_LANGUAGE", "Target language must be explicit"
        )
    if source is not TranslationLanguage.AUTO and source is target:
        raise TranslationError(
            "SOURCE_EQUALS_TARGET", "Source and target languages must be different"
        )


def _mapping(language: TranslationLanguage) -> LanguageMapping:
    try:
        return LANGUAGE_MAPPINGS[language]
    except KeyError as exc:
        raise TranslationError(
            "SOURCE_LANGUAGE_UNDETERMINED",
            "AUTO does not have a concrete model or BabelDOC language code",
        ) from exc
