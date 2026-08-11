from __future__ import annotations

import base64
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
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAgAAAQABAAD//gAQTGF2YzYyLjExLjEwMAD/2wBDAAgEBAQEBAUFBQUFBQYGBgYGBgYGBgYGBgYHBwcICAgHBwcGBgcHCAgICAkJCQgICAgJCQoKCgwMCwsODg4RERT/xABLAAEBAAAAAAAAAAAAAAAAAAAACAEBAAAAAAAAAAAAAAAAAAAAABABAAAAAAAAAAAAAAAAAAAAABEBAAAAAAAAAAAAAAAAAAAAAP/AABEIAAIAAgMBIgACEQADEQD/2gAMAwEAAhEDEQA/AJ/AB//Z"
)


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


def test_import_uses_atomic_no_replace_publish_and_recovers_same_digest(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import_root, artifact_root = roots
    (import_root / "shot.jpg").write_bytes(JPEG)
    links: list[tuple[str | bytes, str | bytes]] = []
    real_link = os.link

    def recording_link(source: str | bytes, destination: str | bytes, **kwargs: object) -> None:
        links.append((source, destination))
        real_link(source, destination, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "link", recording_link)
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
    assert len(links) == 1
    assert (artifact_root / imported.relative_path).read_bytes() == JPEG
    assert not tuple((artifact_root / "asset/shot-001").glob(".asset-attempt-*.tmp"))

    recovered = importer.import_asset(
        source_path="shot.jpg",
        destination_path=imported.relative_path,
        expected_mime_type="image/jpeg",
        expected_sha256=imported.sha256,
    )
    assert recovered.recovered is True
    assert len(links) == 2


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


def test_import_rejects_damaged_and_polyglot_media(roots: tuple[Path, Path]) -> None:
    import_root, _ = roots
    importer = _importer(roots)
    samples = {
        "truncated.jpg": JPEG[:-8],
        "polyglot.jpg": JPEG + b"PK\x03\x04hidden-archive",
        "fake.mp4": b"\x00\x00\x00\x0cftypisom\x00\x00\x00\x08moov\x00\x00\x00\x08mdat",
    }
    for name, content in samples.items():
        (import_root / name).write_bytes(content)
        mime_type = "video/mp4" if name.endswith(".mp4") else "image/jpeg"
        with pytest.raises(UnsupportedAssetMediaType):
            importer.import_asset(
                source_path=name,
                destination_path=f"asset/{name}",
                expected_mime_type=mime_type,
                expected_sha256=_digest(content),
            )


def test_existing_hard_link_is_never_accepted_as_immutable_target(
    roots: tuple[Path, Path], tmp_path: Path
) -> None:
    import_root, artifact_root = roots
    (import_root / "shot.jpg").write_bytes(JPEG)
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(JPEG)
    destination = artifact_root / "asset/shot.jpg"
    destination.parent.mkdir()
    os.link(outside, destination)

    with pytest.raises(UnsafeAssetPath):
        _importer(roots).import_asset(
            source_path="shot.jpg",
            destination_path="asset/shot.jpg",
            expected_mime_type="image/jpeg",
            expected_sha256=_digest(JPEG),
        )


def test_non_cooperating_target_race_fails_closed(
    roots: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_root, _ = roots
    (import_root / "shot.jpg").write_bytes(JPEG)
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(JPEG)
    real_link = os.link
    raced = False

    def racing_link(source: str | bytes, destination: str | bytes, **kwargs: object) -> None:
        nonlocal raced
        if not raced:
            raced = True
            os.symlink(outside, destination, dir_fd=kwargs["dst_dir_fd"])  # type: ignore[arg-type]
        real_link(source, destination, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "link", racing_link)
    with pytest.raises(UnsafeAssetPath):
        _importer(roots).import_asset(
            source_path="shot.jpg",
            destination_path="asset/shot.jpg",
            expected_mime_type="image/jpeg",
            expected_sha256=_digest(JPEG),
        )


def test_parent_swap_between_copy_and_publish_fails_closed(
    roots: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_root, artifact_root = roots
    (import_root / "shot.jpg").write_bytes(JPEG)
    importer = _importer(roots)
    real_verify = importer._decoder.verify
    swapped = False

    def swapping_verify(descriptor: int, mime_type: str) -> None:
        nonlocal swapped
        real_verify(descriptor, mime_type)
        if not swapped:
            swapped = True
            (artifact_root / "asset").rename(artifact_root / "orphaned-asset")
            (artifact_root / "asset").symlink_to(tmp_path)

    monkeypatch.setattr(importer._decoder, "verify", swapping_verify)
    with pytest.raises(UnsafeAssetPath):
        importer.import_asset(
            source_path="shot.jpg",
            destination_path="asset/shot.jpg",
            expected_mime_type="image/jpeg",
            expected_sha256=_digest(JPEG),
        )
    assert not (tmp_path / "shot.jpg").exists()


def test_competing_hard_link_after_publish_is_detected(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    import_root, artifact_root = roots
    (import_root / "shot.jpg").write_bytes(JPEG)
    real_unlink = os.unlink
    raced = False

    def racing_unlink(name: str | bytes, **kwargs: object) -> None:
        nonlocal raced
        if not raced and str(name).startswith(".asset-attempt-"):
            raced = True
            os.link(
                "shot.jpg",
                "stolen-hard-link.jpg",
                src_dir_fd=kwargs["dir_fd"],  # type: ignore[arg-type]
                dst_dir_fd=kwargs["dir_fd"],  # type: ignore[arg-type]
                follow_symlinks=False,
            )
        real_unlink(name, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "unlink", racing_unlink)
    with pytest.raises(UnsafeAssetPath):
        _importer(roots).import_asset(
            source_path="shot.jpg",
            destination_path="asset/shot.jpg",
            expected_mime_type="image/jpeg",
            expected_sha256=_digest(JPEG),
        )
    assert (artifact_root / "asset/shot.jpg").stat().st_nlink == 2


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


@pytest.mark.parametrize(
    "uri",
    [
        "file:///private/asset.jpg",
        "http://example.test/a",
        "../a",
        "https://example.test/a?token=secret",
        "https://example.test/a#private",
        "urn:ecc:asset:1?token=secret",
        "urn:ecc:asset:1#private",
    ],
)
def test_manifest_rejects_unsafe_provenance_uris(uri: str) -> None:
    with pytest.raises(ValueError):
        AssetSource(uri=uri, title="Unsafe source")
