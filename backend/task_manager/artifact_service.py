"""Task-owned publication and safe resolution of tool-generated files."""

from __future__ import annotations

import hashlib
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tool.artifacts import ArtifactRef

from .pipeline.store import create_file_artifact


class TaskArtifactPublisher:
    def __init__(
        self,
        *,
        root: Path,
        task_id: str,
        run_id: str,
        stage_run_id: str,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.task_id = task_id
        self.run_id = run_id
        self.stage_run_id = stage_run_id

    async def publish(
        self,
        source_path: Path,
        *,
        sequence: int,
        mime: str,
    ) -> ArtifactRef:
        source = Path(source_path).resolve()
        if not source.is_file():
            raise ValueError("Artifact source is not a file.")

        artifact_id = uuid.uuid4().hex
        date_path = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        prefix = "image" if mime.startswith("image/") else "artifact"
        name = f"{prefix}-{sequence:03d}{source.suffix.lower()}"
        artifact_dir = self.root / date_path / artifact_id
        target = artifact_dir / name
        artifact_dir.mkdir(parents=True, exist_ok=False)
        try:
            shutil.copyfile(source, target)
            checksum = _sha256(target)
            content_uri = target.relative_to(self.root).as_posix()
            await create_file_artifact(
                artifact_id=artifact_id,
                task_id=self.task_id,
                run_id=self.run_id,
                stage_run_id=self.stage_run_id,
                content_uri=content_uri,
                checksum=checksum,
                summary=name,
                metadata={"name": name, "mime": mime},
            )
        except Exception:
            shutil.rmtree(artifact_dir, ignore_errors=True)
            raise

        return ArtifactRef(
            id=artifact_id,
            name=name,
            mime=mime,
            url=f"/task-manager/artifacts/{artifact_id}/content",
        )


def resolve_artifact_path(root: Path, content_uri: str) -> Path:
    root_path = Path(root).expanduser().resolve()
    target = (root_path / content_uri).resolve()
    if target == root_path or root_path not in target.parents:
        raise ValueError("Artifact path is outside the configured storage root.")
    return target


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
