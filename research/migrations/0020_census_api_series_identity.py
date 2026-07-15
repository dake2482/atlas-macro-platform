from __future__ import annotations

from copy import deepcopy

from django.db import migrations

SERIES_ALIASES = {
    "census-mrts-44x72-sm-sa": "census-api-mrts-44x72-sm-sa",
    "census-mrts-44x72-sm-sa-mom": "census-api-mrts-44x72-sm-sa-mom",
    "census-mrts-44x72-sm-sa-yoy": "census-api-mrts-44x72-sm-sa-yoy",
}


def _rename_series_value(value):
    raw_value = str(value)
    qualified = SERIES_ALIASES.get(raw_value.lower(), raw_value)
    return qualified.upper() if raw_value.isupper() else qualified


def _rename_mapping_keys(value):
    if not isinstance(value, dict):
        return value
    return {
        SERIES_ALIASES.get(str(key).lower(), str(key)): nested
        for key, nested in value.items()
    }


def _rewrite_observation_metadata(metadata):
    updated = deepcopy(metadata or {})
    input_series = updated.get("input_series")
    if isinstance(input_series, list):
        updated["input_series"] = [_rename_series_value(key) for key in input_series]
    input_series_id = updated.get("input_series_id")
    if input_series_id is not None:
        updated["input_series_id"] = _rename_series_value(input_series_id)
    lineage = updated.get("input_lineage")
    if isinstance(lineage, list):
        rewritten = []
        for item in lineage:
            entry = deepcopy(item)
            if isinstance(entry, dict) and entry.get("series_key") is not None:
                entry["series_key"] = _rename_series_value(entry["series_key"])
            rewritten.append(entry)
        updated["input_lineage"] = rewritten
    return updated


def qualify_census_api_series(apps, schema_editor):
    Source = apps.get_model("research", "Source")
    SeriesDefinition = apps.get_model("research", "SeriesDefinition")
    Observation = apps.get_model("research", "Observation")
    IngestionRun = apps.get_model("research", "IngestionRun")

    source = Source.objects.filter(key="census").first()
    if source is None:
        return

    for legacy_key, qualified_key in SERIES_ALIASES.items():
        legacy = SeriesDefinition.objects.filter(
            source_id=source.pk,
            key=legacy_key,
        ).first()
        collision = SeriesDefinition.objects.filter(key=qualified_key).first()
        if legacy is not None and collision is not None and collision.pk != legacy.pk:
            raise RuntimeError(
                f"cannot qualify Census API series {legacy_key}: {qualified_key} exists"
            )
        if legacy is None:
            if collision is not None and collision.source_id != source.pk:
                raise RuntimeError(
                    f"Census API series alias {qualified_key} belongs to another source"
                )
            continue
        legacy.key = qualified_key
        legacy.save(update_fields=["key", "updated_at"])

    for run in IngestionRun.objects.filter(source_id=source.pk).iterator():
        metadata = deepcopy(run.metadata or {})
        changed = False
        for field in ("latest_value_dates", "series_date_coverage"):
            if field in metadata:
                rewritten = _rename_mapping_keys(metadata[field])
                if rewritten != metadata[field]:
                    metadata[field] = rewritten
                    changed = True
        if changed:
            run.metadata = metadata
            run.save(update_fields=["metadata", "updated_at"])

    for observation in Observation.objects.filter(source_id=source.pk).iterator():
        metadata = _rewrite_observation_metadata(observation.metadata)
        if metadata != (observation.metadata or {}):
            observation.metadata = metadata
            observation.save(update_fields=["metadata"])


class Migration(migrations.Migration):
    dependencies = [("research", "0019_expand_ingestion_run_dataset")]

    operations = [
        migrations.RunPython(
            qualify_census_api_series,
            reverse_code=migrations.RunPython.noop,
        )
    ]
