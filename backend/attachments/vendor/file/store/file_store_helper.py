"""Local by default; optional PAI storage backends are loaded on demand."""
import os
from functools import lru_cache

@lru_cache(maxsize=1)
def get_file_store():
    backend = os.getenv("AITUGE_ATTACHMENT_STORAGE", "local")
    if backend == "local":
        from .local_store import LocalFileStore
        return LocalFileStore(os.getenv("AITUGE_ATTACHMENT_STORAGE_ROOT", "localdata/attachments"))
    # Optional providers retain their original source/configuration contract.
    if backend == "oss":
        from .oss_store import OssFileStore
        return OssFileStore(bucket=os.environ["OSS_BUCKET"], endpoint=os.environ["OSS_ENDPOINT"], prefix_path=os.getenv("OSS_PREFIX", "attachments"))
    if backend == "bailian":
        from .bailian_file_store import BailianFileStore
        return BailianFileStore()
    raise ValueError(f"Unknown attachment storage: {backend}")

class _StoreProxy:
    def __getattr__(self, name):
        return getattr(get_file_store(), name)
file_store = _StoreProxy()
