from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class AssetImportError(Exception):
    """Safe base error: messages intentionally contain no input or storage paths."""


class UnsafeAssetPath(AssetImportError):
    pass


class UnsupportedAssetMediaType(AssetImportError):
    pass


class AssetDigestMismatch(AssetImportError):
    pass


class ImmutableAssetConflict(AssetImportError):
    pass


_EXTENSIONS = {
    "image/jpeg": frozenset({".jpg", ".jpeg"}),
    "image/png": frozenset({".png"}),
    "video/mp4": frozenset({".mp4"}),
    "audio/wav": frozenset({".wav"}),
    "audio/mpeg": frozenset({".mp3"}),
}


def _relative_path(raw: str) -> PurePosixPath:
    if not raw or "\x00" in raw or "\\" in raw:
        raise UnsafeAssetPath("unsafe asset path")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in raw.split("/")):
        raise UnsafeAssetPath("unsafe asset path")
    if path.as_posix() != raw:
        raise UnsafeAssetPath("asset path must be normalized")
    return path


def _has_symlink(root: Path, parts: tuple[str, ...]) -> bool:
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _detected_mime(header: bytes) -> str | None:
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return "video/mp4"
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return "audio/wav"
    if header.startswith(b"ID3") or (
        len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0
    ):
        return "audio/mpeg"
    return None


@dataclass(frozen=True, slots=True)
class ImportedAsset:
    relative_path: str
    sha256: str
    mime_type: str
    byte_size: int
    recovered: bool


class LocalAssetImporter:
    def __init__(self, *, import_root: Path, artifact_root: Path) -> None:
        self._import_root = import_root.resolve(strict=True)
        self._artifact_root = artifact_root.resolve(strict=True)
        if not self._import_root.is_dir() or not self._artifact_root.is_dir():
            raise ValueError("asset roots must be directories")
        self._lock_root = self._artifact_root / ".import-locks"
        if self._lock_root.is_symlink():
            raise UnsafeAssetPath("asset lock directory is a symbolic link")
        self._lock_root.mkdir(mode=0o700, exist_ok=True)
        if not self._lock_root.resolve().is_relative_to(self._artifact_root):
            raise UnsafeAssetPath("asset lock directory escaped artifact root")

    def import_asset(
        self,
        *,
        source_path: str,
        destination_path: str,
        expected_mime_type: str,
        expected_sha256: str,
    ) -> ImportedAsset:
        source_relative = _relative_path(source_path)
        destination_relative = _relative_path(destination_path)
        if expected_mime_type not in _EXTENSIONS:
            raise UnsupportedAssetMediaType("unsupported asset media type")
        if destination_relative.suffix.lower() not in _EXTENSIONS[expected_mime_type]:
            raise UnsupportedAssetMediaType("asset extension does not match media type")
        if len(expected_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in expected_sha256
        ):
            raise ValueError("expected_sha256 must be a lowercase SHA-256 digest")

        if _has_symlink(self._import_root, source_relative.parts):
            raise UnsafeAssetPath("symbolic links are not accepted for asset import")
        source = self._import_root.joinpath(*source_relative.parts)
        try:
            source_resolved = source.resolve(strict=True)
        except (FileNotFoundError, RuntimeError) as error:
            raise UnsafeAssetPath("asset source is unavailable") from error
        if not source_resolved.is_relative_to(self._import_root) or not source_resolved.is_file():
            raise UnsafeAssetPath("asset source escaped import root")

        if _has_symlink(self._artifact_root, destination_relative.parts[:-1]):
            raise UnsafeAssetPath("artifact destination contains a symbolic link")
        destination = self._artifact_root.joinpath(*destination_relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination_parent = destination.parent.resolve(strict=True)
        if not destination_parent.is_relative_to(self._artifact_root):
            raise UnsafeAssetPath("artifact destination escaped artifact root")
        destination = destination_parent / destination.name

        temporary_name: str | None = None
        try:
            try:
                source_descriptor = os.open(source_resolved, os.O_RDONLY | os.O_NOFOLLOW)
            except OSError as error:
                raise UnsafeAssetPath("asset source is unavailable") from error
            with os.fdopen(source_descriptor, "rb") as source_stream:
                header = source_stream.read(32)
                if _detected_mime(header) != expected_mime_type:
                    raise UnsupportedAssetMediaType("asset content does not match media type")
                source_stream.seek(0)
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=destination.parent,
                    prefix=".asset-attempt-",
                    suffix=".tmp",
                    delete=False,
                ) as temporary:
                    temporary_name = temporary.name
                    shutil.copyfileobj(source_stream, temporary, length=1024 * 1024)
                    temporary.flush()
                    os.fsync(temporary.fileno())

            temporary_path = Path(temporary_name)
            actual_sha256, byte_size = _sha256(temporary_path)
            if actual_sha256 != expected_sha256:
                raise AssetDigestMismatch("asset digest changed during import")

            lock_digest = hashlib.sha256(destination_relative.as_posix().encode()).hexdigest()
            lock_path = self._lock_root / f"{lock_digest}.lock"
            with lock_path.open("a+b") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if destination.is_symlink():
                    raise UnsafeAssetPath("artifact destination is a symbolic link")
                if destination.exists():
                    try:
                        existing_sha256, existing_size = _sha256(destination)
                    except OSError as error:
                        raise UnsafeAssetPath("artifact destination is unsafe") from error
                    if existing_sha256 != expected_sha256:
                        raise ImmutableAssetConflict("immutable asset destination already exists")
                    return ImportedAsset(
                        relative_path=destination_relative.as_posix(),
                        sha256=existing_sha256,
                        mime_type=expected_mime_type,
                        byte_size=existing_size,
                        recovered=True,
                    )
                os.replace(temporary_path, destination)
                temporary_name = None
                directory_fd = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            return ImportedAsset(
                relative_path=destination_relative.as_posix(),
                sha256=actual_sha256,
                mime_type=expected_mime_type,
                byte_size=byte_size,
                recovered=False,
            )
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
