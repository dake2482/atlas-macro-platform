from __future__ import annotations

import pytest
from django.core.management import call_command


@pytest.fixture(autouse=True)
def static_storage_without_production_manifest(settings):
    """Templates should render in tests before collectstatic creates a manifest."""

    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture(scope="session")
def seeded_platform(django_db_setup, django_db_blocker):
    """Load the deterministic offline product-shape dataset once per test run."""

    with django_db_blocker.unblock():
        call_command("seed_platform", allow_demo_data=True, verbosity=0)
    return None
