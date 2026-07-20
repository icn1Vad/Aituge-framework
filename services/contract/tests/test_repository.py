from __future__ import annotations

import pytest

from contract.persistence.models import DocumentBlockCreate
from contract.persistence.postgres.repository import ContractRepository


def _block(**overrides) -> DocumentBlockCreate:
    values = {
        "block_id": "block-1",
        "block_no": 1,
        "block_type": "paragraph",
        "text": "合同正文",
        "page_number": 1,
        "paragraph_no": 1,
        "char_start": 0,
        "char_end": 4,
        "heading_path": [],
        "metadata": {},
    }
    values.update(overrides)
    return DocumentBlockCreate(**values)


def test_parse_blocks_require_consecutive_numbers() -> None:
    with pytest.raises(ValueError, match="consecutive"):
        ContractRepository._validate_blocks([_block(block_no=2)])


def test_parse_blocks_require_unique_ids() -> None:
    with pytest.raises(ValueError, match="unique"):
        ContractRepository._validate_blocks([_block(), _block(block_no=2)])


def test_parse_block_range_uses_python_code_point_length() -> None:
    ContractRepository._validate_blocks([_block(text="甲方A", char_end=3)])

    with pytest.raises(ValueError, match="range length"):
        ContractRepository._validate_blocks([_block(text="甲方A", char_end=4)])
