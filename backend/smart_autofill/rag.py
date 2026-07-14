from __future__ import annotations


class CombinedRagStore:
    """Read-only composition of stores implementing the framework RAG contract."""

    def __init__(self, *stores) -> None:
        self.stores = stores

    def load_models(self):
        knowledgebases, files, chunks = [], [], []
        for store in self.stores:
            store_kbs, store_files, store_chunks = store.load_models()
            knowledgebases.extend(store_kbs)
            files.extend(store_files)
            chunks.extend(store_chunks)
        return knowledgebases, files, chunks
