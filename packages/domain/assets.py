from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import urlsplit

from packages.domain.pipeline import LogicalArtifactRef
from packages.domain.types import FrozenJsonValue, freeze_json_mapping


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _safe_provenance_uri(value: str, field_name: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme == "https"
        and parts.netloc
        and parts.username is None
        and parts.password is None
    ):
        return value
    if parts.scheme == "urn" and parts.path:
        return value
    raise ValueError(f"{field_name} must be an https URL or URN")


class LicenseType(StrEnum):
    OWNED = "owned"
    COMMISSIONED = "commissioned"
    ROYALTY_FREE = "royalty_free"
    CREATIVE_COMMONS = "creative_commons"
    UNKNOWN = "unknown"


class RightsScope(StrEnum):
    COMMERCIAL_USE = "commercial_use"
    MODIFICATION = "modification"
    DISTRIBUTION = "distribution"


class RightsIssueCode(StrEnum):
    LICENSE_UNKNOWN = "asset.license_unknown"
    LICENSE_EXPIRED = "asset.license_expired"
    RIGHTS_SCOPE_MISSING = "asset.rights_scope_missing"


@dataclass(frozen=True, slots=True)
class AssetSource:
    uri: str
    title: str
    creator: str | None = None

    def __post_init__(self) -> None:
        _safe_provenance_uri(self.uri, "source uri")
        if not self.title.strip():
            raise ValueError("source title is required")
        if self.creator is not None and not self.creator.strip():
            raise ValueError("source creator must not be blank")


@dataclass(frozen=True, slots=True)
class AssetRights:
    license_type: LicenseType
    scopes: frozenset[RightsScope]
    proof_uri: str | None = None
    attribution: str | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.license_type, LicenseType):
            raise ValueError("license_type must be a LicenseType")
        scopes = frozenset(self.scopes)
        if any(not isinstance(scope, RightsScope) for scope in scopes):
            raise ValueError("scopes must contain RightsScope values")
        if self.proof_uri is not None:
            _safe_provenance_uri(self.proof_uri, "proof uri")
        if self.license_type is not LicenseType.UNKNOWN and self.proof_uri is None:
            raise ValueError("known asset rights require a proof uri")
        if self.attribution is not None and not self.attribution.strip():
            raise ValueError("attribution must not be blank")
        expires_at = _utc(self.expires_at) if self.expires_at is not None else None
        object.__setattr__(self, "scopes", scopes)
        object.__setattr__(self, "expires_at", expires_at)


@dataclass(frozen=True, slots=True)
class AssetManifestEntry:
    artifact: LogicalArtifactRef
    source: AssetSource
    rights: AssetRights
    mime_type: str
    byte_size: int

    def __post_init__(self) -> None:
        if self.artifact.kind.value != "media" or not self.artifact.logical_key.startswith(
            "asset/"
        ):
            raise ValueError("manifest entries must reference asset MEDIA artifacts")
        if self.mime_type not in {
            "image/jpeg",
            "image/png",
            "audio/mpeg",
            "audio/wav",
            "video/mp4",
        }:
            raise ValueError("asset MIME type is not supported")
        if (
            isinstance(self.byte_size, bool)
            or not isinstance(self.byte_size, int)
            or self.byte_size < 1
        ):
            raise ValueError("asset byte_size must be positive")


@dataclass(frozen=True, slots=True)
class AssetRightsIssue:
    code: RightsIssueCode
    logical_key: str


@dataclass(frozen=True, slots=True)
class AssetManifest:
    schema_version: str
    entries: tuple[AssetManifestEntry, ...]

    def __post_init__(self) -> None:
        if self.schema_version != "asset_rights_v1":
            raise ValueError("unsupported asset rights schema version")
        entries = tuple(sorted(self.entries, key=lambda item: item.artifact.logical_key))
        if not entries:
            raise ValueError("asset manifest entries must not be empty")
        if len({item.artifact.logical_key for item in entries}) != len(entries):
            raise ValueError("asset manifest logical keys must be unique")
        object.__setattr__(self, "entries", entries)

    def rights_issues(self, *, at: datetime) -> tuple[AssetRightsIssue, ...]:
        checked_at = _utc(at)
        required = frozenset(RightsScope)
        issues: list[AssetRightsIssue] = []
        for entry in self.entries:
            rights = entry.rights
            if rights.license_type is LicenseType.UNKNOWN:
                issues.append(
                    AssetRightsIssue(RightsIssueCode.LICENSE_UNKNOWN, entry.artifact.logical_key)
                )
            if rights.expires_at is not None and rights.expires_at <= checked_at:
                issues.append(
                    AssetRightsIssue(RightsIssueCode.LICENSE_EXPIRED, entry.artifact.logical_key)
                )
            if not required.issubset(rights.scopes):
                issues.append(
                    AssetRightsIssue(
                        RightsIssueCode.RIGHTS_SCOPE_MISSING,
                        entry.artifact.logical_key,
                    )
                )
        return tuple(sorted(issues, key=lambda item: (item.logical_key, item.code.value)))

    def public_metadata(self) -> FrozenJsonValue:
        entries: list[dict[str, FrozenJsonValue]] = []
        for entry in self.entries:
            rights = entry.rights
            entries.append(
                {
                    "logical_key": entry.artifact.logical_key,
                    "artifact_id": str(entry.artifact.artifact_id),
                    "version": entry.artifact.version,
                    "sha256": entry.artifact.sha256,
                    "mime_type": entry.mime_type,
                    "byte_size": entry.byte_size,
                    "source": {
                        "uri": entry.source.uri,
                        "title": entry.source.title,
                        "creator": entry.source.creator,
                    },
                    "rights": {
                        "license_type": rights.license_type.value,
                        "scopes": tuple(sorted(scope.value for scope in rights.scopes)),
                        "proof_uri": rights.proof_uri,
                        "attribution": rights.attribution,
                        "expires_at": (
                            rights.expires_at.isoformat() if rights.expires_at is not None else None
                        ),
                    },
                }
            )
        return freeze_json_mapping(
            {"schema_version": self.schema_version, "entries": tuple(entries)}
        )
