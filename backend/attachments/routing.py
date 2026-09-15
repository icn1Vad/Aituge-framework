"""Shared routing for optional MinerU chat-attachment extraction."""
import os

MINERU_ATTACHMENT_EXTENSIONS = {'.pdf', '.png', '.jpg', '.jpeg', '.webp'}


def uses_mineru_for_attachment(extension: str | None) -> bool:
    return (
        os.getenv('AITUGE_MINERU_ENABLED', 'true').lower() == 'true'
        and (extension or '').lower() in MINERU_ATTACHMENT_EXTENSIONS
    )
