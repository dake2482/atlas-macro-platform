"""Deterministic private evidence bundles for multi-response providers.

The bundle keeps exact upstream bytes private while preserving a credential-free
manifest of response roles, canonical URLs, content types and request/response
witnesses.  It is intentionally transport-only: source-specific parsers remain
responsible for replaying each role into normalized records.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlparse

EVIDENCE_BUNDLE_SCHEMA_VERSION = 1
EVIDENCE_BUNDLE_CONTENT_TYPE = "application/vnd.atlas.raw-evidence+zip"
MAX_EVIDENCE_BUNDLE_BYTES = 128 * 1024 * 1024
MAX_EVIDENCE_BLOB_BYTES = 64 * 1024 * 1024
_ROLE_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}")
_SECRET_KEY_RE = re.compile(
    r"(?:api[-_]?key|apikey|registration[-_]?key|token|secret|password|authorization)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class EvidenceResponse:
    role: str
    url: str
    content_type: str
    raw_bytes: bytes
    request_witness: dict[str, Any] = field(default_factory=dict)
    response_witness: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ParsedEvidenceBundle:
    manifest: dict[str, Any]
    responses: dict[str, bytes]


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError) as exc:
        raise ValueError("evidence witness is not canonical JSON") from exc


def _contains_secret_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            _is_secret_key(key)
            or _contains_secret_key(nested)
            for key, nested in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_key(item) for item in value)
    return False


def _is_secret_key(value: Any) -> bool:
    raw_key = str(value).strip()
    compact_key = re.sub(r"[^a-z0-9]", "", raw_key.casefold())
    return (
        compact_key == "key"
        or compact_key.endswith(("apikey", "registrationkey"))
        or bool(_SECRET_KEY_RE.search(raw_key))
    )


def _validated_public_url(raw_url: str) -> str:
    parsed = urlparse(str(raw_url))
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or any(
            _is_secret_key(key)
            for key, _value in parse_qsl(parsed.query, keep_blank_values=True)
        )
    ):
        raise ValueError("evidence URL is not a credential-free HTTPS URL")
    return parsed.geturl()


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o600 << 16
    return info


def build_evidence_bundle(
    *,
    provider: str,
    dataset: str,
    responses: list[EvidenceResponse] | tuple[EvidenceResponse, ...],
) -> tuple[bytes, dict[str, Any]]:
    """Build a deterministic ZIP with one manifest and deduplicated blobs."""

    if not provider or not dataset or not responses:
        raise ValueError("evidence bundle identity and responses are required")
    manifest_responses: list[dict[str, Any]] = []
    blobs: dict[str, bytes] = {}
    seen_roles: set[str] = set()
    for response in responses:
        role = str(response.role)
        if not _ROLE_RE.fullmatch(role) or role in seen_roles:
            raise ValueError("evidence response role is invalid or duplicated")
        seen_roles.add(role)
        url = _validated_public_url(response.url)
        content_type = str(response.content_type or "application/octet-stream")
        payload = bytes(response.raw_bytes)
        if (
            not payload
            or len(payload) > MAX_EVIDENCE_BLOB_BYTES
            or len(content_type) > 120
            or _contains_secret_key(response.request_witness)
            or _contains_secret_key(response.response_witness)
        ):
            raise ValueError("evidence response bytes, type or witness are invalid")
        request_witness = json.loads(_canonical_json(response.request_witness))
        response_witness = json.loads(_canonical_json(response.response_witness))
        digest = hashlib.sha256(payload).hexdigest()
        blobs.setdefault(digest, payload)
        manifest_responses.append(
            {
                "role": role,
                "url": url,
                "content_type": content_type,
                "sha256": digest,
                "size_bytes": len(payload),
                "blob_path": f"blobs/{digest}.bin",
                "request_witness": request_witness,
                "response_witness": response_witness,
            }
        )
    manifest_responses.sort(key=lambda item: item["role"])
    manifest = {
        "schema_version": EVIDENCE_BUNDLE_SCHEMA_VERSION,
        "provider": str(provider),
        "dataset": str(dataset),
        "responses": manifest_responses,
        "blobs": [
            {
                "sha256": digest,
                "size_bytes": len(blobs[digest]),
                "blob_path": f"blobs/{digest}.bin",
            }
            for digest in sorted(blobs)
        ],
    }
    manifest_bytes = _canonical_json(manifest)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", allowZip64=False) as archive:
        archive.writestr(_zip_info("manifest.json"), manifest_bytes)
        for digest in sorted(blobs):
            archive.writestr(
                _zip_info(f"blobs/{digest}.bin"),
                blobs[digest],
            )
    bundle = output.getvalue()
    if len(bundle) > MAX_EVIDENCE_BUNDLE_BYTES:
        raise ValueError("evidence bundle exceeds the size limit")
    return bundle, {
        "content_type": EVIDENCE_BUNDLE_CONTENT_TYPE,
        "byte_length": len(bundle),
        "sha256": hashlib.sha256(bundle).hexdigest(),
        "evidence_bundle_schema": EVIDENCE_BUNDLE_SCHEMA_VERSION,
        "evidence_roles": [item["role"] for item in manifest_responses],
        "response_count": len(manifest_responses),
        "unique_blob_count": len(blobs),
    }


def parse_evidence_bundle(
    raw_bytes: bytes,
    *,
    expected_provider: str,
    expected_dataset: str,
) -> ParsedEvidenceBundle:
    """Validate a bundle completely and return exact bytes keyed by role."""

    payload = bytes(raw_bytes)
    if not payload or len(payload) > MAX_EVIDENCE_BUNDLE_BYTES:
        raise ValueError("evidence bundle bytes are empty or oversized")
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise ValueError("evidence bundle is not a ZIP archive") from exc
    with archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if len(names) != len(set(names)) or "manifest.json" not in names:
            raise ValueError("evidence bundle entries are duplicated or lack a manifest")
        if any(
            info.is_dir()
            or info.file_size > MAX_EVIDENCE_BLOB_BYTES
            or info.compress_type != zipfile.ZIP_STORED
            for info in infos
        ):
            raise ValueError("evidence bundle entry contract is invalid")
        try:
            manifest_bytes = archive.read("manifest.json")
            manifest = json.loads(manifest_bytes)
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("evidence manifest cannot be decoded") from exc
        if not isinstance(manifest, dict) or manifest_bytes != _canonical_json(manifest):
            raise ValueError("evidence manifest is not canonical JSON")
        if (
            set(manifest)
            != {"schema_version", "provider", "dataset", "responses", "blobs"}
            or manifest.get("schema_version") != EVIDENCE_BUNDLE_SCHEMA_VERSION
            or manifest.get("provider") != expected_provider
            or manifest.get("dataset") != expected_dataset
            or not isinstance(manifest.get("responses"), list)
            or not isinstance(manifest.get("blobs"), list)
        ):
            raise ValueError("evidence manifest identity or schema is invalid")

        blobs: dict[str, bytes] = {}
        expected_names = {"manifest.json"}
        for item in manifest["blobs"]:
            if not isinstance(item, dict) or set(item) != {
                "sha256",
                "size_bytes",
                "blob_path",
            }:
                raise ValueError("evidence blob manifest is invalid")
            digest = str(item.get("sha256") or "")
            blob_path = str(item.get("blob_path") or "")
            if (
                not re.fullmatch(r"[0-9a-f]{64}", digest)
                or blob_path != f"blobs/{digest}.bin"
                or digest in blobs
            ):
                raise ValueError("evidence blob identity is invalid")
            try:
                blob = archive.read(blob_path)
            except KeyError as exc:
                raise ValueError("evidence blob is missing") from exc
            if (
                len(blob) != int(item.get("size_bytes") or -1)
                or hashlib.sha256(blob).hexdigest() != digest
            ):
                raise ValueError("evidence blob hash or size is invalid")
            blobs[digest] = blob
            expected_names.add(blob_path)
        if set(names) != expected_names:
            raise ValueError("evidence bundle contains undeclared entries")

        responses: dict[str, bytes] = {}
        referenced_digests: set[str] = set()
        expected_response_keys = {
            "role",
            "url",
            "content_type",
            "sha256",
            "size_bytes",
            "blob_path",
            "request_witness",
            "response_witness",
        }
        for item in manifest["responses"]:
            if not isinstance(item, dict) or set(item) != expected_response_keys:
                raise ValueError("evidence response manifest is invalid")
            role = str(item.get("role") or "")
            digest = str(item.get("sha256") or "")
            blob = blobs.get(digest)
            if (
                not _ROLE_RE.fullmatch(role)
                or role in responses
                or blob is None
                or item.get("blob_path") != f"blobs/{digest}.bin"
                or int(item.get("size_bytes") or -1) != len(blob)
                or len(str(item.get("content_type") or "")) > 120
                or _validated_public_url(str(item.get("url") or ""))
                != item.get("url")
                or not isinstance(item.get("request_witness"), dict)
                or not isinstance(item.get("response_witness"), dict)
                or _contains_secret_key(item.get("request_witness"))
                or _contains_secret_key(item.get("response_witness"))
            ):
                raise ValueError("evidence response identity or witness is invalid")
            responses[role] = blob
            referenced_digests.add(digest)
        if referenced_digests != set(blobs):
            raise ValueError("evidence blob manifest contains unreferenced blobs")
        if not responses:
            raise ValueError("evidence bundle contains no responses")
        return ParsedEvidenceBundle(manifest=manifest, responses=responses)
