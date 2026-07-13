from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from research.github_catalog import GITHUB_PROJECT_SEEDS
from research.models import GitHubProject, GitHubProjectSnapshot, Source, SourceLicense
from research.providers import GitHubProvider, ProviderResult
from research.services import record_provider_result, store_github_repository


def _result(stars: int, fetched_at: datetime) -> ProviderResult:
    return ProviderResult(
        provider="github",
        dataset="repository:openai/openai-agents-python",
        fetched_at=fetched_at,
        records=[
            {
                "repo": "openai/openai-agents-python",
                "category": "Agent / orchestration",
                "description": "Official OpenAI agents SDK",
                "stars": stars,
                "forks": 100,
                "open_issues": 10,
                "pushed_at": fetched_at.isoformat(),
                "homepage": "https://github.com/openai/openai-agents-python",
            }
        ],
    )


def test_reviewed_github_catalog_has_45_unique_real_repositories():
    repositories = [repo for repo, _ in GITHUB_PROJECT_SEEDS]

    assert len(repositories) == 45
    assert len(set(repositories)) == 45
    assert all(repo.count("/") == 1 for repo in repositories)
    assert not any(repo.startswith("atlas-clean-room/") for repo in repositories)


def test_github_provider_preserves_archive_fork_and_repository_license_metadata():
    def handler(request):
        assert request.url.path == "/repos/example/project"
        return httpx.Response(
            200,
            json={
                "full_name": "example/project",
                "description": "fixture",
                "stargazers_count": 10,
                "forks_count": 2,
                "open_issues_count": 1,
                "pushed_at": "2026-07-08T12:00:00Z",
                "html_url": "https://github.com/example/project",
                "topics": ["ai"],
                "archived": True,
                "fork": False,
                "license": {"spdx_id": "Apache-2.0"},
            },
        )

    provider = GitHubProvider(
        client=httpx.Client(
            base_url="https://api.github.com", transport=httpx.MockTransport(handler)
        )
    )
    result = provider.repository("example/project")

    assert result.ok
    assert result.records[0]["archived"] is True
    assert result.records[0]["is_fork"] is False
    assert result.records[0]["license"] == "Apache-2.0"


@pytest.mark.django_db
def test_github_daily_snapshots_compute_a_real_seven_day_delta():
    first_at = datetime(2026, 7, 1, 12, tzinfo=UTC)
    second_at = datetime(2026, 7, 8, 12, tzinfo=UTC)

    record_provider_result(_result(1_000, first_at), persist=store_github_repository)
    record_provider_result(_result(1_125, second_at), persist=store_github_repository)

    project = GitHubProject.objects.get(repo="openai/openai-agents-python")
    assert project.stars == 1_125
    assert project.stars_7d == 125
    assert project.momentum_score == 125
    assert GitHubProjectSnapshot.objects.filter(project=project).count() == 2
    assert project.source.key == "github"
    assert project.data_as_of == second_at


@pytest.mark.django_db
def test_github_radar_search_category_and_sort_are_shareable_get_filters(client):
    fetched_at = datetime(2026, 7, 8, 12, tzinfo=UTC)
    record_provider_result(_result(1_125, fetched_at), persist=store_github_repository)

    response = client.get(
        "/ai-industry/chain/applications/",
        {"q": "openai", "category": "Agent / orchestration", "sort": "stars"},
    )

    assert response.status_code == 200
    assert "openai/openai-agents-python" in response.content.decode()
    assert response.context["selected_sort"] == "stars"
    assert response.context["query"] == "openai"


def _github_source(*, status=Source.LicenseStatus.OPEN, public_display_allowed=True):
    source, _ = Source.objects.get_or_create(
        key="github",
        defaults={
            "name": "GitHub fixture",
            "license_status": status,
            "redistribution_allowed": public_display_allowed,
        },
    )
    source.license_status = status
    source.redistribution_allowed = public_display_allowed
    source.save(update_fields=["license_status", "redistribution_allowed", "updated_at"])
    source.licenses.filter(is_current=True).update(is_current=False)
    SourceLicense.objects.create(
        source=source,
        is_current=True,
        status=status,
        scope="GitHub fixture licence",
        public_display_allowed=public_display_allowed,
        redistribution_allowed=public_display_allowed,
    )
    return source


def _project(repo, source, *, data_as_of=None, stars=10):
    return GitHubProject.objects.create(
        repo=repo,
        category="Agent / orchestration",
        description=f"{repo} fixture",
        stars=stars,
        homepage=f"https://github.com/{repo}",
        source=source,
        data_as_of=data_as_of,
    )


@pytest.mark.django_db
def test_current_restricted_github_licence_hides_projects_from_radar_and_ai_hub(client):
    source = _github_source(
        status=Source.LicenseStatus.RESTRICTED,
        public_display_allowed=False,
    )
    project = _project("restricted-owner/restricted-project", source)

    radar = client.get("/ai-industry/chain/applications/")
    hub = client.get("/ai-industry/")

    assert radar.status_code == 200
    assert hub.status_code == 200
    assert project.repo not in radar.content.decode()
    assert project.repo not in hub.content.decode()
    assert list(radar.context["projects"]) == []
    assert hub.context["project_count"] == 0


@pytest.mark.django_db
def test_github_radar_and_ai_hub_choose_latest_nonnull_data_as_of(client):
    source = _github_source()
    expected_as_of = datetime(2026, 7, 12, 8, 30, tzinfo=UTC)
    _project("example/dated-project", source, data_as_of=expected_as_of, stars=20)
    _project("example/undated-project", source, data_as_of=None, stars=100)

    radar = client.get("/ai-industry/chain/applications/")
    hub = client.get("/ai-industry/")

    assert radar.status_code == 200
    assert hub.status_code == 200
    assert radar.context["as_of"] == expected_as_of
    assert hub.context["as_of"] == expected_as_of
    assert hub.context["project_count"] == 2
