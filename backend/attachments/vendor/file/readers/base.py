from abc import ABC, abstractmethod
from typing import List, Protocol
from llama_index.core.schema import Document

from backend.attachments.vendor.file.models.file_item import FileItem


class ImageCaptionTool(Protocol):
    """Optional image-caption extension used by rich document readers."""

    def extract_image(self, image_data: bytes) -> str | None:
        ...


class BaseReader(ABC):
    @abstractmethod
    def read(self, file_item: FileItem) -> List[Document]:
        pass
