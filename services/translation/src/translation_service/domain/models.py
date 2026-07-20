from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class TranslationLanguage(StrEnum):
    AUTO = "AUTO"
    ZH_CN = "ZH_CN"
    ZH_TW = "ZH_TW"
    EN = "EN"
    JA = "JA"
    KO = "KO"
    FR = "FR"
    DE = "DE"
    ES = "ES"
    RU = "RU"
    AR = "AR"
    PT = "PT"


class TranslationTaskType(StrEnum):
    TEXT = "TEXT"
    FILE = "FILE"


class TranslationStage(StrEnum):
    VALIDATING = "VALIDATING"
    DETECTING_LANGUAGE = "DETECTING_LANGUAGE"
    CONVERTING_SOURCE = "CONVERTING_SOURCE"
    EXTRACTING = "EXTRACTING"
    TRANSLATING = "TRANSLATING"
    REBUILDING = "REBUILDING"
    PDF_TRANSLATING = "PDF_TRANSLATING"
    CONVERTING_OUTPUT = "CONVERTING_OUTPUT"
    PUBLISHING = "PUBLISHING"
    ARCHIVING = "ARCHIVING"
    OUTPUT_READY = "OUTPUT_READY"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class TranslationFileType(StrEnum):
    PDF = "PDF"
    DOC = "DOC"
    DOCX = "DOCX"


class PdfOutputMode(StrEnum):
    MONO = "MONO"
    DUAL = "DUAL"
    BOTH = "BOTH"


class OfficeOutputFormat(StrEnum):
    SOURCE = "SOURCE"
    DOCX = "DOCX"
    DOC = "DOC"


class GlossaryEntry(BaseModel):
    source: str = Field(min_length=1, max_length=200)
    target: str = Field(min_length=1, max_length=200)


class TextTranslationRequest(BaseModel):
    text: str = Field(min_length=1)
    source_language: TranslationLanguage = TranslationLanguage.AUTO
    target_language: TranslationLanguage
    glossary: list[GlossaryEntry] = Field(default_factory=list, max_length=100)

    @field_validator("target_language")
    @classmethod
    def target_must_be_concrete(
        cls, value: TranslationLanguage
    ) -> TranslationLanguage:
        if value is TranslationLanguage.AUTO:
            raise ValueError("target_language cannot be AUTO")
        return value


class TextTranslationData(BaseModel):
    task_id: str
    run_id: str | None
    status: str
    source_language: TranslationLanguage
    detected_source_language: TranslationLanguage
    target_language: TranslationLanguage
    translated_text: str
    source_sha256: str
    result_sha256: str
    source_character_count: int
    result_character_count: int
    content_retained: bool = False


class ArtifactData(BaseModel):
    artifact_id: str
    output_type: str
    file_name: str
    mime_type: str
    size: int
    sha256: str


class TranslationTaskData(BaseModel):
    task_id: str
    run_id: str | None
    task_type: TranslationTaskType
    status: str
    stage: str
    progress: int
    source_language: TranslationLanguage
    detected_source_language: TranslationLanguage | None = None
    target_language: TranslationLanguage
    source_sha256: str
    source_character_count: int | None = None
    result_sha256: str | None = None
    result_character_count: int | None = None
    content_retained: bool = False
    artifacts: list[ArtifactData] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    created_at: str
    updated_at: str
