from __future__ import annotations

import hashlib
import io
import json
import zipfile

import pytest

from research.raw_evidence import (
    EVIDENCE_BUNDLE_CONTENT_TYPE,
    EvidenceResponse,
    build_evidence_bundle,
    parse_evidence_bundle,
)


def _responses():
    shared = b"same official workbook bytes"
    return (
        EvidenceResponse(
            role="release-index",
            url="https://example.gov/releases/current.html",
            content_type="text/html",
            raw_bytes=b"<html>official release</html>",
            request_witness={"release": "current"},
            response_witness={"etag": "fixture-index"},
        ),
        EvidenceResponse(
            role="current-workbook",
            url="https://example.gov/releases/current.xlsx",
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            raw_bytes=shared,
            request_witness={"table": "current"},
        ),
        EvidenceResponse(
            role="archive-workbook",
            url="https://example.gov/releases/archive.xlsx",
            content_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            raw_bytes=shared,
            request_witness={"table": "archive"},
        ),
    )


def test_evidence_bundle_is_deterministic_and_deduplicates_blobs():
    first, first_metadata = build_evidence_bundle(
        provider="example-official",
        dataset="release-workbooks",
        responses=_responses(),
    )
    second, second_metadata = build_evidence_bundle(
        provider="example-official",
        dataset="release-workbooks",
        responses=tuple(reversed(_responses())),
    )
    assert first == second
    assert first_metadata == second_metadata
    assert first_metadata["content_type"] == EVIDENCE_BUNDLE_CONTENT_TYPE
    assert first_metadata["response_count"] == 3
    assert first_metadata["unique_blob_count"] == 2

    parsed = parse_evidence_bundle(
        first,
        expected_provider="example-official",
        expected_dataset="release-workbooks",
    )
    assert parsed.responses["release-index"] == b"<html>official release</html>"
    assert parsed.responses["current-workbook"] == parsed.responses[
        "archive-workbook"
    ]
    assert [item["role"] for item in parsed.manifest["responses"]] == [
        "archive-workbook",
        "current-workbook",
        "release-index",
    ]


@pytest.mark.parametrize(
    "response",
    (
        EvidenceResponse(
            role="api",
            url="https://api.example.gov/data?api_key=secret",
            content_type="application/json",
            raw_bytes=b"{}",
        ),
        EvidenceResponse(
            role="api",
            url="https://api.example.gov/data",
            content_type="application/json",
            raw_bytes=b"{}",
            request_witness={"registrationKey": "secret"},
        ),
        EvidenceResponse(
            role="api",
            url="https://api.example.gov/data?key=secret",
            content_type="application/json",
            raw_bytes=b"{}",
        ),
        EvidenceResponse(
            role="api",
            url="https://api.example.gov/data?api-key",
            content_type="application/json",
            raw_bytes=b"{}",
        ),
        EvidenceResponse(
            role="api",
            url="https://api.example.gov/data",
            content_type="application/json",
            raw_bytes=b"{}",
            request_witness={"key": "secret"},
        ),
        EvidenceResponse(
            role="api",
            url="https://api.example.gov/data",
            content_type="application/json",
            raw_bytes=b"{}",
            response_witness={"api-key": "secret"},
        ),
    ),
)
def test_evidence_bundle_rejects_credential_bearing_witnesses(response):
    with pytest.raises(ValueError, match="credential|witness"):
        build_evidence_bundle(
            provider="example-official",
            dataset="api",
            responses=(response,),
        )


def test_evidence_bundle_rejects_tampered_or_undeclared_entries():
    bundle, _metadata = build_evidence_bundle(
        provider="example-official",
        dataset="release-workbooks",
        responses=_responses(),
    )
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}

    manifest = json.loads(entries["manifest.json"])
    blob_path = manifest["blobs"][0]["blob_path"]
    entries[blob_path] += b"tamper"
    tampered = io.BytesIO()
    with zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    with pytest.raises(ValueError, match="hash or size|entry contract"):
        parse_evidence_bundle(
            tampered.getvalue(),
            expected_provider="example-official",
            expected_dataset="release-workbooks",
        )

    extra = io.BytesIO()
    with zipfile.ZipFile(extra, "w", compression=zipfile.ZIP_STORED) as archive:
        with zipfile.ZipFile(io.BytesIO(bundle)) as original:
            for name in original.namelist():
                archive.writestr(name, original.read(name))
        archive.writestr("undeclared.bin", b"not in manifest")
    with pytest.raises(ValueError, match="undeclared"):
        parse_evidence_bundle(
            extra.getvalue(),
            expected_provider="example-official",
            expected_dataset="release-workbooks",
        )


def test_evidence_bundle_rejects_unreferenced_manifest_blob():
    bundle, _metadata = build_evidence_bundle(
        provider="example-official",
        dataset="release-workbooks",
        responses=_responses(),
    )
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}

    orphan = b"valid but unreferenced official bytes"
    orphan_digest = hashlib.sha256(orphan).hexdigest()
    orphan_path = f"blobs/{orphan_digest}.bin"
    manifest = json.loads(entries["manifest.json"])
    manifest["blobs"].append(
        {
            "sha256": orphan_digest,
            "size_bytes": len(orphan),
            "blob_path": orphan_path,
        }
    )
    manifest["blobs"].sort(key=lambda item: item["sha256"])
    entries["manifest.json"] = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    entries[orphan_path] = orphan

    tampered = io.BytesIO()
    with zipfile.ZipFile(tampered, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in sorted(entries):
            archive.writestr(name, entries[name])

    with pytest.raises(ValueError, match="unreferenced"):
        parse_evidence_bundle(
            tampered.getvalue(),
            expected_provider="example-official",
            expected_dataset="release-workbooks",
        )
