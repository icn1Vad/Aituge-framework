"""Sync parser bridge to the shared async MinerU adapter (worker thread only)."""
import asyncio
from ..mineru import MinerUConfig, MinerUExtractor

def extract_with_mineru(raw: bytes, file_name: str) -> str:
    return asyncio.run(MinerUExtractor(MinerUConfig.from_env()).extract(raw, file_name)).markdown
