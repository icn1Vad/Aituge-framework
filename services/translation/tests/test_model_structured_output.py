import pytest

from translation_service.config import Settings
from translation_service.domain.models import TranslationLanguage
from translation_service.errors import TranslationError
from translation_service.language.detector import LanguageDetector
from translation_service.model.gateway import parse_json_object


class StubModel:
    def __init__(self, response: str) -> None:
        self.response = response

    async def complete(self, **_: object) -> str:
        return self.response


def settings() -> Settings:
    return Settings(
        internal_token="internal-token-1234",
        model_api_key="test-key",
        babeldoc_internal_token="babeldoc-token-1234",
    )


def test_parse_json_object_accepts_reasoning_wrapper() -> None:
    response = (
        "<think>I should classify the dominant language.</think>\n"
        "```json\n"
        '{"language":"EN","confidence":0.99}\n'
        "```"
    )

    assert parse_json_object(response) == {"language": "EN", "confidence": 0.99}


@pytest.mark.asyncio
async def test_invalid_detection_payload_has_stable_business_error() -> None:
    detector = LanguageDetector(settings(), StubModel("not-json"))  # type: ignore[arg-type]

    with pytest.raises(TranslationError) as error:
        await detector.resolve(
            requested=TranslationLanguage.AUTO,
            target=TranslationLanguage.ZH_CN,
            sample="This sample contains enough English text for language detection.",
            tenant_id="tenant-1",
        )

    assert error.value.code == "SOURCE_LANGUAGE_UNDETERMINED"
