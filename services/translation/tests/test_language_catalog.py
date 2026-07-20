import pytest

from translation_service.domain.models import TranslationLanguage
from translation_service.errors import TranslationError
from translation_service.language.catalog import babeldoc_code, ensure_language_pair


def test_business_language_maps_to_babeldoc_code() -> None:
    assert babeldoc_code(TranslationLanguage.ZH_CN) == "zh-CN"
    assert babeldoc_code(TranslationLanguage.EN) == "en"


def test_auto_never_maps_directly_to_babeldoc() -> None:
    with pytest.raises(TranslationError) as error:
        babeldoc_code(TranslationLanguage.AUTO)
    assert error.value.code == "SOURCE_LANGUAGE_UNDETERMINED"


def test_same_language_pair_is_rejected() -> None:
    with pytest.raises(TranslationError) as error:
        ensure_language_pair(TranslationLanguage.EN, TranslationLanguage.EN)
    assert error.value.code == "SOURCE_EQUALS_TARGET"
