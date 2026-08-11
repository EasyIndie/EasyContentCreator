from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import stat
import subprocess
import zlib
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
_EXPECTED_STREAM = {
    "image/jpeg": "video",
    "image/png": "video",
    "video/mp4": "video",
    "audio/wav": "audio",
    "audio/mpeg": "audio",
}
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW


def _relative_path(raw: str) -> PurePosixPath:
    if not raw or "\x00" in raw or "\\" in raw:
        raise UnsafeAssetPath("unsafe asset path")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in raw.split("/")):
        raise UnsafeAssetPath("unsafe asset path")
    if path.as_posix() != raw:
        raise UnsafeAssetPath("asset path must be normalized")
    return path


def _open_root(path: Path) -> int:
    try:
        descriptor = os.open(path, _DIRECTORY_FLAGS)
    except OSError as error:
        raise UnsafeAssetPath("asset root is unavailable") from error
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise UnsafeAssetPath("asset root is unavailable")
    return descriptor


def _open_directory(parent_fd: int, name: str, *, create: bool) -> int:
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        except OSError as error:
            raise UnsafeAssetPath("artifact destination is unsafe") from error
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        raise UnsafeAssetPath("asset directory is unsafe") from error
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise UnsafeAssetPath("asset directory is unsafe")
    return descriptor


def _walk_parent(root_fd: int, parts: tuple[str, ...], *, create: bool) -> int:
    current_fd = os.dup(root_fd)
    try:
        for part in parts:
            next_fd = _open_directory(current_fd, part, create=create)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _open_regular(parent_fd: int, name: str) -> int:
    try:
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
    except OSError as error:
        raise UnsafeAssetPath("asset file is unavailable") from error
    details = os.fstat(descriptor)
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        os.close(descriptor)
        raise UnsafeAssetPath("asset file is unsafe")
    return descriptor


def _sha256_fd(descriptor: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest(), size


def _copy_fd(source_fd: int, target_fd: int) -> None:
    os.lseek(source_fd, 0, os.SEEK_SET)
    while chunk := os.read(source_fd, 1024 * 1024):
        view = memoryview(chunk)
        while view:
            written = os.write(target_fd, view)
            view = view[written:]
    os.fsync(target_fd)
    os.lseek(target_fd, 0, os.SEEK_SET)


def _read_all(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while chunk := os.read(descriptor, 1024 * 1024):
        chunks.append(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return b"".join(chunks)


def _validate_png(content: bytes) -> None:
    if not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise UnsupportedAssetMediaType("asset content does not match media type")
    offset = 8
    seen_ihdr = False
    seen_iend = False
    while offset < len(content):
        if offset + 12 > len(content):
            raise UnsupportedAssetMediaType("asset media is damaged")
        length = int.from_bytes(content[offset : offset + 4], "big")
        kind = content[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(content):
            raise UnsupportedAssetMediaType("asset media is damaged")
        payload = content[offset + 8 : offset + 8 + length]
        expected_crc = int.from_bytes(content[offset + 8 + length : end], "big")
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != expected_crc:
            raise UnsupportedAssetMediaType("asset media is damaged")
        if not seen_ihdr and (kind != b"IHDR" or length != 13):
            raise UnsupportedAssetMediaType("asset media is damaged")
        seen_ihdr = True
        offset = end
        if kind == b"IEND":
            if length != 0 or offset != len(content):
                raise UnsupportedAssetMediaType("asset media contains trailing data")
            seen_iend = True
            break
    if not seen_ihdr or not seen_iend:
        raise UnsupportedAssetMediaType("asset media is damaged")


def _validate_jpeg(content: bytes) -> None:
    if len(content) < 4 or not content.startswith(b"\xff\xd8"):
        raise UnsupportedAssetMediaType("asset media is damaged")
    offset = 2
    in_scan = False
    while offset < len(content):
        if content[offset] != 0xFF:
            if in_scan:
                offset += 1
                continue
            raise UnsupportedAssetMediaType("asset media is damaged")
        while offset < len(content) and content[offset] == 0xFF:
            offset += 1
        if offset >= len(content):
            raise UnsupportedAssetMediaType("asset media is damaged")
        marker = content[offset]
        offset += 1
        if in_scan and marker == 0x00:
            continue
        if marker == 0xD9:
            if offset != len(content):
                raise UnsupportedAssetMediaType("asset media contains trailing data")
            return
        if marker in range(0xD0, 0xD8) or marker in {0x01, 0xD8}:
            continue
        if offset + 2 > len(content):
            raise UnsupportedAssetMediaType("asset media is damaged")
        segment_length = int.from_bytes(content[offset : offset + 2], "big")
        if segment_length < 2 or offset + segment_length > len(content):
            raise UnsupportedAssetMediaType("asset media is damaged")
        offset += segment_length
        in_scan = marker == 0xDA
    raise UnsupportedAssetMediaType("asset media is damaged")


def _validate_riff(content: bytes) -> None:
    if len(content) < 12 or content[:4] != b"RIFF" or content[8:12] != b"WAVE":
        raise UnsupportedAssetMediaType("asset content does not match media type")
    if int.from_bytes(content[4:8], "little") + 8 != len(content):
        raise UnsupportedAssetMediaType("asset media contains trailing or truncated data")


def _validate_mp4(content: bytes) -> None:
    offset = 0
    kinds: set[bytes] = set()
    while offset < len(content):
        if offset + 8 > len(content):
            raise UnsupportedAssetMediaType("asset media is damaged")
        size = int.from_bytes(content[offset : offset + 4], "big")
        kind = content[offset + 4 : offset + 8]
        header_size = 8
        if size == 1:
            if offset + 16 > len(content):
                raise UnsupportedAssetMediaType("asset media is damaged")
            size = int.from_bytes(content[offset + 8 : offset + 16], "big")
            header_size = 16
        if size < header_size or offset + size > len(content):
            raise UnsupportedAssetMediaType("asset media is damaged")
        kinds.add(kind)
        offset += size
    if (
        offset != len(content)
        or b"ftyp" not in kinds
        or b"moov" not in kinds
        or b"mdat" not in kinds
    ):
        raise UnsupportedAssetMediaType("asset media is damaged")


def _validate_mp3(content: bytes) -> None:
    offset = 0
    end = len(content)
    if content.startswith(b"ID3"):
        if len(content) < 10 or any(byte & 0x80 for byte in content[6:10]):
            raise UnsupportedAssetMediaType("asset media is damaged")
        tag_size = sum(
            byte << shift for byte, shift in zip(content[6:10], (21, 14, 7, 0), strict=True)
        )
        offset = 10 + tag_size + (10 if content[5] & 0x10 else 0)
    if end >= 128 and content[end - 128 : end - 125] == b"TAG":
        end -= 128
    frames = 0
    bitrate_v1 = {
        1: (32, 64, 96, 128, 160, 192, 224, 256, 288, 320, 352, 384, 416, 448),
        2: (32, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 384),
        3: (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
    }
    bitrate_v2 = {
        1: (32, 48, 56, 64, 80, 96, 112, 128, 144, 160, 176, 192, 224, 256),
        2: (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
        3: (8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    }
    while offset < end:
        if offset + 4 > end:
            raise UnsupportedAssetMediaType("asset media contains trailing data")
        header = int.from_bytes(content[offset : offset + 4], "big")
        if header >> 21 != 0x7FF:
            raise UnsupportedAssetMediaType("asset media contains trailing data")
        version = (header >> 19) & 0b11
        layer_bits = (header >> 17) & 0b11
        bitrate_index = (header >> 12) & 0xF
        sample_index = (header >> 10) & 0b11
        padding = (header >> 9) & 1
        if version == 1 or layer_bits == 0 or bitrate_index in {0, 15} or sample_index == 3:
            raise UnsupportedAssetMediaType("asset media is damaged")
        layer = 4 - layer_bits
        tables = bitrate_v1 if version == 3 else bitrate_v2
        bitrate = tables[layer][bitrate_index - 1] * 1000
        sample_rates = (44100, 48000, 32000)
        sample_rate = sample_rates[sample_index]
        if version == 2:
            sample_rate //= 2
        elif version == 0:
            sample_rate //= 4
        if layer == 1:
            frame_size = (12 * bitrate // sample_rate + padding) * 4
        else:
            coefficient = 72 if layer == 3 and version != 3 else 144
            frame_size = coefficient * bitrate // sample_rate + padding
        if frame_size < 4 or offset + frame_size > end:
            raise UnsupportedAssetMediaType("asset media is damaged")
        offset += frame_size
        frames += 1
    if frames == 0:
        raise UnsupportedAssetMediaType("asset media is damaged")


def _structural_validate(descriptor: int, mime_type: str) -> None:
    content = _read_all(descriptor)
    if mime_type == "image/png":
        _validate_png(content)
    elif mime_type == "image/jpeg":
        _validate_jpeg(content)
    elif mime_type == "audio/wav":
        _validate_riff(content)
    elif mime_type == "video/mp4":
        _validate_mp4(content)
    elif mime_type == "audio/mpeg":
        _validate_mp3(content)


class _DecoderVerifier:
    def __init__(self, *, ffprobe: Path, ffmpeg: Path, timeout_seconds: float) -> None:
        self._ffprobe = ffprobe
        self._ffmpeg = ffmpeg
        self._timeout_seconds = timeout_seconds
        for binary in (ffprobe, ffmpeg):
            if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
                raise ValueError("media decoder binaries must be absolute executable files")

    def verify(self, descriptor: int, mime_type: str) -> None:
        fd_path = f"/dev/fd/{descriptor}"
        probe = self._run(
            (
                str(self._ffprobe),
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "json",
                fd_path,
            ),
            descriptor,
        )
        try:
            document = json.loads(probe.stdout)
            streams = document["streams"]
            stream_types = {item["codec_type"] for item in streams}
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise UnsupportedAssetMediaType("asset media probe failed") from error
        expected_stream = _EXPECTED_STREAM[mime_type]
        if expected_stream not in stream_types or not stream_types.issubset({expected_stream}):
            raise UnsupportedAssetMediaType("asset media streams do not match media type")
        self._run(
            (
                str(self._ffmpeg),
                "-v",
                "error",
                "-xerror",
                "-err_detect",
                "explode",
                "-nostdin",
                "-i",
                fd_path,
                "-f",
                "null",
                "-",
            ),
            descriptor,
        )

    def _run(self, command: tuple[str, ...], descriptor: int) -> subprocess.CompletedProcess[str]:
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                pass_fds=(descriptor,),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise UnsupportedAssetMediaType("asset media decoder failed") from error
        finally:
            os.lseek(descriptor, 0, os.SEEK_SET)
        if completed.returncode != 0:
            raise UnsupportedAssetMediaType("asset media decoder rejected content")
        return completed


@dataclass(frozen=True, slots=True)
class ImportedAsset:
    relative_path: str
    sha256: str
    mime_type: str
    byte_size: int
    recovered: bool


class LocalAssetImporter:
    def __init__(
        self,
        *,
        import_root: Path,
        artifact_root: Path,
        ffprobe: Path | None = None,
        ffmpeg: Path | None = None,
        decoder_timeout_seconds: float = 30.0,
    ) -> None:
        self._import_root = import_root.absolute()
        self._artifact_root = artifact_root.absolute()
        import_fd = _open_root(self._import_root)
        artifact_fd = _open_root(self._artifact_root)
        os.close(import_fd)
        os.close(artifact_fd)
        self._decoder = _DecoderVerifier(
            ffprobe=ffprobe or _platform_binary("ffprobe"),
            ffmpeg=ffmpeg or _platform_binary("ffmpeg"),
            timeout_seconds=decoder_timeout_seconds,
        )

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

        import_fd = _open_root(self._import_root)
        artifact_fd = _open_root(self._artifact_root)
        try:
            source_parent_fd = _walk_parent(import_fd, source_relative.parts[:-1], create=False)
            try:
                source_fd = _open_regular(source_parent_fd, source_relative.name)
            finally:
                os.close(source_parent_fd)
            try:
                return self._import_open_source(
                    source_fd=source_fd,
                    artifact_fd=artifact_fd,
                    destination=destination_relative,
                    expected_mime_type=expected_mime_type,
                    expected_sha256=expected_sha256,
                )
            finally:
                os.close(source_fd)
        finally:
            os.close(import_fd)
            os.close(artifact_fd)

    def _import_open_source(
        self,
        *,
        source_fd: int,
        artifact_fd: int,
        destination: PurePosixPath,
        expected_mime_type: str,
        expected_sha256: str,
    ) -> ImportedAsset:
        parent_fd = _walk_parent(artifact_fd, destination.parts[:-1], create=True)
        temp_name = f".asset-attempt-{secrets.token_hex(16)}.tmp"
        temp_fd: int | None = None
        try:
            temp_fd = os.open(
                temp_name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            _copy_fd(source_fd, temp_fd)
            actual_sha256, byte_size = _sha256_fd(temp_fd)
            if actual_sha256 != expected_sha256:
                raise AssetDigestMismatch("asset digest changed during import")
            _structural_validate(temp_fd, expected_mime_type)
            self._decoder.verify(temp_fd, expected_mime_type)
            return self._publish(
                artifact_fd=artifact_fd,
                parent_fd=parent_fd,
                destination=destination,
                temp_name=temp_name,
                expected_sha256=expected_sha256,
                expected_mime_type=expected_mime_type,
                byte_size=byte_size,
            )
        finally:
            if temp_fd is not None:
                os.close(temp_fd)
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)

    def _publish(
        self,
        *,
        artifact_fd: int,
        parent_fd: int,
        destination: PurePosixPath,
        temp_name: str,
        expected_sha256: str,
        expected_mime_type: str,
        byte_size: int,
    ) -> ImportedAsset:
        lock_root_fd = _open_directory(artifact_fd, ".import-locks", create=True)
        lock_digest = hashlib.sha256(destination.as_posix().encode()).hexdigest()
        lock_fd: int | None = None
        try:
            lock_fd = os.open(
                f"{lock_digest}.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=lock_root_fd,
            )
            lock_stat = os.fstat(lock_fd)
            if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
                raise UnsafeAssetPath("asset lock is unsafe")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)

            rechecked_parent_fd = _walk_parent(artifact_fd, destination.parts[:-1], create=False)
            try:
                expected_parent = os.fstat(parent_fd)
                actual_parent = os.fstat(rechecked_parent_fd)
                if (expected_parent.st_dev, expected_parent.st_ino) != (
                    actual_parent.st_dev,
                    actual_parent.st_ino,
                ):
                    raise UnsafeAssetPath("artifact destination changed during import")
                try:
                    os.link(
                        temp_name,
                        destination.name,
                        src_dir_fd=rechecked_parent_fd,
                        dst_dir_fd=rechecked_parent_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    return self._recover_existing(
                        rechecked_parent_fd,
                        destination,
                        expected_sha256,
                        expected_mime_type,
                    )
                os.unlink(temp_name, dir_fd=rechecked_parent_fd)
                os.fsync(rechecked_parent_fd)
                published = self._recover_existing(
                    rechecked_parent_fd,
                    destination,
                    expected_sha256,
                    expected_mime_type,
                    recovered=False,
                )
            finally:
                os.close(rechecked_parent_fd)
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            os.close(lock_root_fd)
        if published.byte_size != byte_size:
            raise UnsafeAssetPath("published asset size changed")
        return published

    def _recover_existing(
        self,
        parent_fd: int,
        destination: PurePosixPath,
        expected_sha256: str,
        expected_mime_type: str,
        *,
        recovered: bool = True,
    ) -> ImportedAsset:
        try:
            existing_fd = _open_regular(parent_fd, destination.name)
        except UnsafeAssetPath as error:
            raise UnsafeAssetPath("artifact destination is unsafe") from error
        try:
            before = os.fstat(existing_fd)
            existing_sha256, existing_size = _sha256_fd(existing_fd)
            if existing_sha256 != expected_sha256:
                raise ImmutableAssetConflict("immutable asset destination already exists")
            _structural_validate(existing_fd, expected_mime_type)
            self._decoder.verify(existing_fd, expected_mime_type)
            verified_sha256, verified_size = _sha256_fd(existing_fd)
            after = os.fstat(existing_fd)
            if _file_identity(before) != _file_identity(after):
                raise UnsafeAssetPath("artifact destination changed during verification")
            if (
                verified_sha256 != expected_sha256
                or verified_sha256 != existing_sha256
                or verified_size != existing_size
            ):
                raise UnsafeAssetPath("artifact destination changed during verification")
        finally:
            os.close(existing_fd)
        return ImportedAsset(
            relative_path=destination.as_posix(),
            sha256=existing_sha256,
            mime_type=expected_mime_type,
            byte_size=existing_size,
            recovered=recovered,
        )


def _file_identity(details: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_nlink,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def _platform_binary(name: str) -> Path:
    candidates = (
        Path("/usr/bin") / name,
        Path("/usr/local/bin") / name,
        Path("/opt/homebrew/bin") / name,
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise ValueError("required media decoder binary is unavailable")
