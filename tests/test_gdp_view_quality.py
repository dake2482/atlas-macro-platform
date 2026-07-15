from __future__ import annotations

import uuid
from copy import deepcopy

import pytest
from django.utils import timezone

from research.models import DashboardSnapshot, Observation, Source, SourceLicense


@pytest.mark.django_db
@pytest.mark.parametrize(
    "publication_state",
    ["retained_failure", "transition_pending", "natural_expiry"],
)
def test_gdp_non_current_state_renders_every_component_stale_without_persisting(
    client,
    monkeypatch,
    publication_state,
):
    source = Source.objects.create(
        key="fixture-gdp-view-quality",
        name="GDP view quality fixture",
        license_status=Source.LicenseStatus.OPEN,
    )
    SourceLicense.objects.create(
        source=source,
        status=Source.LicenseStatus.OPEN,
        scope="GDP view quality regression fixture",
        public_display_allowed=True,
        derived_display_allowed=True,
        historical_storage_allowed=True,
        is_current=True,
    )
    snapshot = DashboardSnapshot.objects.create(
        key="gdp",
        title="GDP",
        summary="GDP presentation fixture",
        as_of=timezone.now(),
        batch_id=uuid.uuid4(),
        quality_status=Observation.Quality.FRESH,
        source=source,
        is_published=True,
        data={
            "contract_version": 2,
            "fingerprint": "fixture-hashed-payload",
            "source_keys": [source.key],
            "metrics": [
                {
                    "key": "real-gdp-growth",
                    "label": "Real GDP growth",
                    "value": 2.1,
                    "display_value": "2.10%",
                    "quality_status": Observation.Quality.FRESH,
                    "source_keys": [source.key],
                }
            ],
            "charts": [
                {
                    "key": "real-gdp-history",
                    "title": "Real GDP history",
                    "kind": "line",
                    "data": [{"date": "2026Q1", "value": 2.1}],
                    "quality_status": Observation.Quality.FRESH,
                    "source_keys": [source.key],
                }
            ],
            "sections": [
                {
                    "key": "revision-path",
                    "title": "Revision path",
                    "status": Observation.Quality.FRESH,
                    "quality_status": Observation.Quality.FRESH,
                    "rows": [],
                    "source_keys": [source.key],
                }
            ],
        },
    )
    stored_data = deepcopy(snapshot.data)

    def select_fixture(candidates):
        selected = list(candidates)[0]
        selected.gdp_publication_state = publication_state
        return selected

    monkeypatch.setattr("research.views.select_public_gdp_snapshot", select_fixture)

    response = client.get("/economy/gdp/")

    assert response.status_code == 200
    assert response.context["snapshot"].quality_status == Observation.Quality.STALE
    assert all(
        item["quality_status"] == Observation.Quality.STALE
        for item in response.context["metrics"]
    )
    assert all(
        item["quality_status"] == Observation.Quality.STALE
        for item in response.context["charts"]
    )
    assert all(
        item["status"] == Observation.Quality.STALE
        and item["quality_status"] == Observation.Quality.STALE
        for item in response.context["sections"]
    )

    snapshot.refresh_from_db()
    assert snapshot.quality_status == Observation.Quality.FRESH
    assert snapshot.data == stored_data
