from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.utils import timezone

from research.models import (
    DashboardSnapshot,
    EvidenceItem,
    Invalidation,
    MetricSnapshot,
    Observation,
    Thesis,
    Trigger,
)
from research.services import ensure_source
from research.thesis_publication import (
    DAILY_EVIDENCE_COMPONENT_CONTRACT_VERSIONS,
    DAILY_EVIDENCE_COMPONENT_KEYS,
    DAILY_EVIDENCE_CONTRACT_VERSION,
    DAILY_EVIDENCE_LEGACY_CONTRACT_VERSION,
    DAILY_EVIDENCE_PREFERRED_METRICS,
    DAILY_EVIDENCE_V2_STRICT_FORMULA_VERSIONS,
    component_data_fingerprint,
    component_reference_fingerprint,
    daily_evidence_component_set_fingerprint,
    daily_evidence_payload_fingerprint,
    publish_theses,
)


def _fingerprint(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def build_daily_components(
    marker: str,
    *,
    now: datetime | None = None,
    component_contract_versions: dict[str, int] | None = None,
) -> tuple[list[DashboardSnapshot], list[MetricSnapshot]]:
    current_time = now or timezone.now()
    versions = component_contract_versions or {
        page_key: DAILY_EVIDENCE_LEGACY_CONTRACT_VERSION
        for page_key in DAILY_EVIDENCE_COMPONENT_KEYS
    }
    source = ensure_source("internal")
    components: list[DashboardSnapshot] = []
    metrics: list[MetricSnapshot] = []
    for index, page_key in enumerate(DAILY_EVIDENCE_COMPONENT_KEYS):
        batch_id = uuid.uuid4()
        component_batch_id = uuid.uuid4()
        raw_metric_key = DAILY_EVIDENCE_PREFERRED_METRICS[page_key]
        fingerprint = _fingerprint(f"{marker}-{page_key}")
        fresh_until = current_time + timedelta(days=1)
        component_data = {
            "demo": False,
            "contract_version": versions[page_key],
            "publication_batch_id": str(batch_id),
            "fingerprint": fingerprint,
            "component_batches": [str(component_batch_id)],
            "source_keys": [source.key],
            "fresh_until": fresh_until.isoformat(),
        }
        if versions == DAILY_EVIDENCE_COMPONENT_CONTRACT_VERSIONS:
            component_data.update(
                {
                    "formula_version": DAILY_EVIDENCE_V2_STRICT_FORMULA_VERSIONS.get(
                        page_key,
                        f"fixture-{page_key}-v{versions[page_key]}",
                    ),
                    "payload_integrity_hash": _fingerprint(
                        f"{marker}-{page_key}-payload"
                    ),
                    "test_daily_component": True,
                }
            )
        component = DashboardSnapshot.objects.create(
            key=page_key,
            title=f"{marker} {page_key}",
            as_of=current_time - timedelta(hours=1),
            batch_id=batch_id,
            quality_status=Observation.Quality.ESTIMATED,
            summary="Verified component fixture",
            data=component_data,
            source=source,
            is_published=True,
        )
        components.append(component)
        metric = MetricSnapshot.objects.create(
            key=f"{page_key}-{raw_metric_key.lower()}",
            label=f"{page_key} evidence",
            value=Decimal(str(index + 1)),
            display_value=f"{index + 1}.00",
            unit="index",
            value_date=current_time - timedelta(hours=2),
            as_of=current_time - timedelta(hours=1),
            fetched_at=current_time - timedelta(minutes=30),
            batch_id=batch_id,
            source=source,
            quality_status=Observation.Quality.ESTIMATED,
            license_scope=source.license_scope,
            metadata={
                "source_key": source.key,
                "component_batch_id": str(component_batch_id),
            },
        )
        metrics.append(metric)
        component_data = dict(component.data)
        component_data["metrics"] = [
            {
                "key": raw_metric_key,
                "value": str(metric.value),
                "display_value": metric.display_value,
                "unit": metric.unit,
                "value_date": metric.value_date.isoformat(),
                "as_of": metric.as_of.isoformat(),
                "fetched_at": metric.fetched_at.isoformat(),
                "batch_id": str(component_batch_id),
                "quality_status": metric.quality_status,
                "source_key": metric.source.key,
                "license_scope": metric.license_scope,
                "fresh_until": fresh_until.isoformat(),
            }
        ]
        component.data = component_data
        component.save(update_fields=["data", "updated_at"])

    DashboardSnapshot.objects.filter(pk__in=[item.pk for item in components]).update(
        created_at=current_time - timedelta(microseconds=2),
        updated_at=current_time - timedelta(microseconds=2),
    )
    MetricSnapshot.objects.filter(pk__in=[item.pk for item in metrics]).update(
        created_at=current_time - timedelta(microseconds=1),
        updated_at=current_time - timedelta(microseconds=1),
    )
    for component in components:
        component.refresh_from_db()
    for metric in metrics:
        metric.refresh_from_db()
    for component, metric in zip(components, metrics, strict=True):
        component_data = dict(component.data)
        metric_payload = dict(component_data["metrics"][0])
        metric_payload.update(
            {
                "value_date": metric.value_date.isoformat(),
                "as_of": metric.as_of.isoformat(),
                "fetched_at": metric.fetched_at.isoformat(),
            }
        )
        component_data["metrics"] = [metric_payload]
        component.data = component_data
        component.save(update_fields=["data", "updated_at"])
    return components, metrics


def build_daily_evidence(
    marker: str,
    *,
    now: datetime | None = None,
    contract_version: int = DAILY_EVIDENCE_LEGACY_CONTRACT_VERSION,
) -> tuple[DashboardSnapshot, list[MetricSnapshot]]:
    current_time = now or timezone.now()
    source = ensure_source("internal")
    component_versions = (
        DAILY_EVIDENCE_COMPONENT_CONTRACT_VERSIONS
        if contract_version == DAILY_EVIDENCE_CONTRACT_VERSION
        else {
            page_key: DAILY_EVIDENCE_LEGACY_CONTRACT_VERSION
            for page_key in DAILY_EVIDENCE_COMPONENT_KEYS
        }
    )
    components, metrics = build_daily_components(
        marker,
        now=current_time,
        component_contract_versions=component_versions,
    )

    component_references = []
    for component in components:
        reference = {
            "page_key": component.key,
            "demo": False,
            "contract_version": component.data["contract_version"],
            "snapshot_id": component.pk,
            "publication_batch_id": str(component.batch_id),
            "fingerprint": component.data["fingerprint"],
            "as_of": component.as_of.isoformat(),
            "quality_status": component.quality_status,
            "source_key": component.source.key,
            "component_batches": sorted(
                [
                    str(component.batch_id),
                    *component.data["component_batches"],
                ]
            ),
            "source_keys": [component.source.key],
            "fresh_until": component.data["fresh_until"],
            "metrics": component.data["metrics"],
            "component_data_sha256": component_data_fingerprint(component.data),
        }
        if contract_version == DAILY_EVIDENCE_CONTRACT_VERSION:
            reference.update(
                {
                    "formula_version": component.data.get("formula_version"),
                    "payload_integrity_hash": component.data.get(
                        "payload_integrity_hash"
                    ),
                }
            )
        reference["component_payload_sha256"] = component_reference_fingerprint(reference)
        component_references.append(reference)

    parent_batch = uuid.uuid4()
    parent_data = {
        "demo": False,
        "contract_version": contract_version,
        "publication_batch_id": str(parent_batch),
        "research_date": timezone.localdate(current_time).isoformat(),
        "required_components": list(DAILY_EVIDENCE_COMPONENT_KEYS),
        "component_snapshots": component_references,
        "component_batches": sorted(
            {
                batch
                for reference in component_references
                for batch in reference["component_batches"]
            }
        ),
        "source_keys": [source.key],
        "evidence_metric_ids": [item.pk for item in metrics],
        "evidence_items": [
            {
                "component": component.key,
                "metric_id": metric.pk,
                "key": metric.key,
                "component_metric_key": component.data["metrics"][0]["key"],
                "value": str(metric.value),
                "display_value": metric.display_value,
                "unit": metric.unit,
                "value_date": metric.value_date.isoformat(),
                "as_of": metric.as_of.isoformat(),
                "fetched_at": metric.fetched_at.isoformat(),
                "batch_id": str(metric.batch_id),
                "quality_status": metric.quality_status,
                "source_key": metric.source.key,
                "license_scope": metric.license_scope,
                "fresh_until": (current_time + timedelta(days=1)).isoformat(),
            }
            for component, metric in zip(components, metrics, strict=True)
        ],
    }
    parent_data["component_set_sha256"] = daily_evidence_component_set_fingerprint(
        parent_data
    )
    parent_data["fingerprint"] = daily_evidence_payload_fingerprint(parent_data)
    parent = DashboardSnapshot.objects.create(
        key="daily-evidence",
        title=f"{marker} daily evidence",
        as_of=current_time - timedelta(hours=1),
        batch_id=parent_batch,
        quality_status=Observation.Quality.ESTIMATED,
        summary="Verified daily evidence fixture",
        data=parent_data,
        source=source,
        is_published=True,
    )
    DashboardSnapshot.objects.filter(pk=parent.pk).update(
        created_at=current_time,
        updated_at=current_time,
    )
    parent.refresh_from_db()
    return parent, metrics


def build_complete_thesis(
    marker: str,
    *,
    report_date=None,
    now: datetime | None = None,
    publish: bool = True,
    evidence_contract_version: int = DAILY_EVIDENCE_LEGACY_CONTRACT_VERSION,
) -> Thesis:
    if now is None and report_date is not None:
        current_time = timezone.make_aware(
            datetime.combine(report_date, time(hour=12)),
            timezone.get_current_timezone(),
        )
    else:
        current_time = now or timezone.now()
    report_date = report_date or timezone.localdate(current_time)
    snapshot, metrics = build_daily_evidence(
        marker,
        now=current_time,
        contract_version=evidence_contract_version,
    )
    thesis = Thesis.objects.create(
        date=report_date,
        regime=marker,
        confidence="中高",
        summary=f"{marker} reviewed summary",
        evidence=["LEGACY-EVIDENCE-MUST-NOT-RENDER"],
        triggers=["LEGACY-TRIGGER-MUST-NOT-RENDER"],
        invalidation="LEGACY-INVALIDATION-MUST-NOT-RENDER",
        source_snapshot=snapshot,
    )
    for index, metric in enumerate(metrics, start=1):
        EvidenceItem.objects.create(
            thesis=thesis,
            label=f"Evidence {index}",
            body=f"{marker} evidence body {index}",
            source=metric.source,
            source_url=f"https://example.org/{marker.lower()}/evidence-{index}",
            snapshot=metric,
            value_date=metric.value_date,
            confidence=Decimal("0.900"),
        )
    Trigger.objects.create(
        thesis=thesis,
        name=f"{marker} trigger",
        condition="The verified threshold is crossed",
        display_threshold="> 1",
    )
    Invalidation.objects.create(
        thesis=thesis,
        condition=f"{marker} invalidation condition",
    )
    if publish:
        outcome = publish_theses(
            Thesis.objects.filter(pk=thesis.pk),
            reviewer="test-reviewer",
            now=current_time,
        )
        assert outcome.ok, outcome.errors
        thesis.refresh_from_db()
    return thesis
