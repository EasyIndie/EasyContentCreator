from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from packages.domain import (
    ArtifactKind,
    ArtifactRef,
    AssetManifest,
    AssetManifestEntry,
    AssetRights,
    AssetSource,
    LicenseType,
    LogicalArtifactRef,
    RightsIssueCode,
    RightsScope,
)
from packages.media import (
    AssetDigestMismatch,
    ImmutableAssetConflict,
    LocalAssetImporter,
    UnsafeAssetPath,
    UnsupportedAssetMediaType,
)

NOW = datetime(2026, 8, 11, 8, tzinfo=UTC)
JPEG = b"\xff\xd8\xff\xe0" + b"safe-jpeg-payload" + b"\xff\xd9"


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    import_root = tmp_path / "incoming"
    artifact_root = tmp_path / "artifacts"
    import_root.mkdir()
    artifact_root.mkdir()
    return import_root, artifact_root


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _importer(roots: tuple[Path, Path]) -> LocalAssetImporter:
    return LocalAssetImporter(import_root=roots[0], artifact_root=roots[1])


def test_import_uses_atomic_rename_and_recovers_same_digest(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import_root, artifact_root = roots
    (import_root / "shot.jpg").write_bytes(JPEG)
    replacements: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def recording_replace(source: str | Path, destination: str | Path) -> None:
        replacements.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", recording_replace)
    importer = _importer(roots)
    imported = importer.import_asset(
        source_path="shot.jpg",
        destination_path="asset/shot-001/image.jpg",
        expected_mime_type="image/jpeg",
        expected_sha256=_digest(JPEG),
    )

    assert imported.relative_path == "asset/shot-001/image.jpg"
    assert imported.sha256 == _digest(JPEG)
    assert imported.byte_size == len(JPEG)
    assert imported.recovered is False
    assert len(replacements) == 1
    assert replacements[0][0].parent == replacements[0][1].parent
    assert (artifact_root / imported.relative_path).read_bytes() == JPEG
    assert not tuple((artifact_root / "asset/shot-001").glob(".asset-attempt-*.tmp"))

    recovered = importer.import_asset(
        source_path="shot.jpg",
        destination_path=imported.relative_path,
        expected_mime_type="image/jpeg",
        expected_sha256=imported.sha256,
    )
    assert recovered.recovered is True
    assert len(replacements) == 1


@pytest.mark.parametrize(
    "source_path,destination_path",
    [
        ("/private/shot.jpg", "asset/shot.jpg"),
        ("../shot.jpg", "asset/shot.jpg"),
        ("shot.jpg", "../asset/shot.jpg"),
        ("shot.jpg", "/asset/shot.jpg"),
        ("shot\x00.jpg", "asset/shot.jpg"),
        ("shot.jpg", "asset\\shot.jpg"),
        ("shot.jpg", "asset//shot.jpg"),
    ],
)
def test_import_rejects_unsafe_paths(
    roots: tuple[Path, Path], source_path: str, destination_path: str
) -> None:
    (roots[0] / "shot.jpg").write_bytes(JPEG)
    with pytest.raises(UnsafeAssetPath):
        _importer(roots).import_asset(
            source_path=source_path,
            destination_path=destination_path,
            expected_mime_type="image/jpeg",
            expected_sha256=_digest(JPEG),
        )


def test_import_rejects_source_and_destination_symlinks(
    roots: tuple[Path, Path], tmp_path: Path
) -> None:
    import_root, artifact_root = roots
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(JPEG)
    (import_root / "escape.jpg").symlink_to(outside)
    importer = _importer(roots)
    with pytest.raises(UnsafeAssetPath):
        importer.import_asset(
            source_path="escape.jpg",
            destination_path="asset/shot.jpg",
            expected_mime_type="image/jpeg",
            expected_sha256=_digest(JPEG),
        )

    (import_root / "shot.jpg").write_bytes(JPEG)
    safe_parent = artifact_root / "asset"
    safe_parent.mkdir()
    (safe_parent / "shot.jpg").symlink_to(outside)
    with pytest.raises(UnsafeAssetPath):
        importer.import_asset(
            source_path="shot.jpg",
            destination_path="asset/shot.jpg",
            expected_mime_type="image/jpeg",
            expected_sha256=_digest(JPEG),
        )
    (artifact_root / "escape").symlink_to(tmp_path)
    with pytest.raises(UnsafeAssetPath):
        importer.import_asset(
            source_path="shot.jpg",
            destination_path="escape/shot.jpg",
            expected_mime_type="image/jpeg",
            expected_sha256=_digest(JPEG),
        )


def test_import_rejects_mime_disguise_digest_drift_and_immutable_conflict(
    roots: tuple[Path, Path],
) -> None:
    import_root, artifact_root = roots
    disguised = import_root / "disguised.jpg"
    disguised.write_bytes(b"not an image")
    importer = _importer(roots)
    with pytest.raises(UnsupportedAssetMediaType):
        importer.import_asset(
            source_path="disguised.jpg",
            destination_path="asset/disguised.jpg",
            expected_mime_type="image/jpeg",
            expected_sha256=_digest(disguised.read_bytes()),
        )

    source = import_root / "shot.jpg"
    source.write_bytes(JPEG)
    with pytest.raises(AssetDigestMismatch):
        importer.import_asset(
            source_path="shot.jpg",
            destination_path="asset/shot.jpg",
            expected_mime_type="image/jpeg",
            expected_sha256="0" * 64,
        )

    destination = artifact_root / "asset/shot.jpg"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"different immutable bytes")
    with pytest.raises(ImmutableAssetConflict):
        importer.import_asset(
            source_path="shot.jpg",
            destination_path="asset/shot.jpg",
            expected_mime_type="image/jpeg",
            expected_sha256=_digest(JPEG),
        )


def test_import_errors_and_logs_do_not_reveal_paths(
    roots: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    secret_name = "customer-secret-shot.jpg"
    (roots[0] / secret_name).write_bytes(JPEG)
    with pytest.raises(AssetDigestMismatch) as captured:
        _importer(roots).import_asset(
            source_path=secret_name,
            destination_path="private-campaign/secret.jpg",
            expected_mime_type="image/jpeg",
            expected_sha256="0" * 64,
        )
    combined = str(captured.value) + caplog.text
    assert secret_name not in combined
    assert str(roots[0]) not in combined
    assert str(roots[1]) not in combined


def _artifact(logical_key: str, suffix: int) -> LogicalArtifactRef:
    return LogicalArtifactRef(
        ArtifactRef(
            UUID(f"00000000-0000-0000-0000-{suffix:012d}"),
            1,
            ArtifactKind.MEDIA,
            f"{suffix:x}".rjust(64, "0"),
        ),
        logical_key,
    )


def _entry(logical_key: str, suffix: int, rights: AssetRights) -> AssetManifestEntry:
    return AssetManifestEntry(
        artifact=_artifact(logical_key, suffix),
        source=AssetSource(
            uri=f"https://assets.example.test/{suffix}",
            title=f"Asset {suffix}",
            creator="Example Studio",
        ),
        rights=rights,
        mime_type="image/jpeg",
        byte_size=100 + suffix,
    )


def test_rights_manifest_blocks_unknown_expired_and_incomplete_scope() -> None:
    manifest = AssetManifest(
        "asset_rights_v1",
        (
            _entry(
                "asset/shot-002",
                2,
                AssetRights(
                    LicenseType.UNKNOWN,
                    frozenset(),
                    expires_at=NOW - timedelta(seconds=1),
                ),
            ),
            _entry(
                "asset/shot-001",
                1,
                AssetRights(
                    LicenseType.ROYALTY_FREE,
                    frozenset({RightsScope.COMMERCIAL_USE}),
                    proof_uri="urn:ecc:license:asset-1",
                    expires_at=NOW + timedelta(days=1),
                ),
            ),
        ),
    )

    assert [(issue.logical_key, issue.code) for issue in manifest.rights_issues(at=NOW)] == [
        ("asset/shot-001", RightsIssueCode.RIGHTS_SCOPE_MISSING),
        ("asset/shot-002", RightsIssueCode.LICENSE_EXPIRED),
        ("asset/shot-002", RightsIssueCode.LICENSE_UNKNOWN),
        ("asset/shot-002", RightsIssueCode.RIGHTS_SCOPE_MISSING),
    ]
    metadata = manifest.public_metadata()
    serialized = str(metadata)
    assert "storage_path" not in serialized
    assert "file:" not in serialized
    assert "asset/shot-001" in serialized


def test_complete_current_rights_are_qc_ready() -> None:
    manifest = AssetManifest(
        "asset_rights_v1",
        (
            _entry(
                "asset/shot-001",
                1,
                AssetRights(
                    LicenseType.COMMISSIONED,
                    frozenset(RightsScope),
                    proof_uri="urn:ecc:license:commission-1",
                    attribution="Example Studio",
                    expires_at=NOW + timedelta(days=30),
                ),
            ),
        ),
    )
    assert manifest.rights_issues(at=NOW) == ()


@pytest.mark.parametrize("uri", ["file:///private/asset.jpg", "http://example.test/a", "../a"])
def test_manifest_rejects_unsafe_provenance_uris(uri: str) -> None:
    with pytest.raises(ValueError):
        AssetSource(uri=uri, title="Unsafe source")
