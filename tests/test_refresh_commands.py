from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from research.tasks import refresh_official_sources, refresh_prates_sources


@pytest.mark.parametrize(
    ("command_name", "target", "summary", "message"),
    [
        (
            "refresh_official_data",
            "research.management.commands.refresh_official_data.refresh_official_data",
            {
                "runs": [{"status": "partial", "row_count": 0}],
                "dashboard_keys": [],
            },
            "official source refreshes were incomplete",
        ),
        (
            "refresh_treasury_curve_data",
            "research.management.commands.refresh_treasury_curve_data.refresh_treasury_curve_data",
            {
                "runs": [{"status": "failed", "row_count": 0}],
                "dashboard_keys": [],
                "stale_dashboard_keys": ["yield-curve", "real-rates"],
            },
            "Treasury annual curve refreshes were incomplete",
        ),
        (
            "refresh_h41_data",
            "research.management.commands.refresh_h41_data.refresh_h41_data",
            {
                "runs": [
                    {
                        "source": "federal-reserve",
                        "dataset": "h41",
                        "status": "failed",
                        "row_count": 0,
                        "error": "upstream archive unavailable",
                    }
                ],
                "dashboard_keys": [],
            },
            "upstream archive unavailable",
        ),
        (
            "refresh_h8_data",
            "research.management.commands.refresh_h8_data.refresh_h8_data",
            {
                "runs": [
                    {
                        "source": "federal-reserve",
                        "dataset": "h8",
                        "status": "failed",
                        "row_count": 0,
                        "error": "H.8 archive unavailable",
                    }
                ],
                "dashboard_keys": [],
            },
            "H.8 archive unavailable",
        ),
        (
            "refresh_prates_data",
            "research.management.commands.refresh_prates_data.refresh_prates_data",
            {
                "runs": [
                    {
                        "source": "federal-reserve",
                        "dataset": "prates:iorb",
                        "status": "failed",
                        "row_count": 0,
                        "error": "PRATES unavailable",
                    }
                ],
                "dashboard_keys": [],
            },
            "PRATES unavailable",
        ),
        (
            "refresh_h10_data",
            "research.management.commands.refresh_h10_data.refresh_h10_data",
            {
                "runs": [
                    {
                        "source": "federal-reserve",
                        "dataset": "h10",
                        "status": "partial",
                        "row_count": 3,
                        "error": "",
                    }
                ],
                "dashboard_keys": [],
            },
            "H.10 refresh incomplete",
        ),
        (
            "refresh_credit_data",
            "research.management.commands.refresh_credit_data.refresh_credit_official_data",
            {
                "runs": [
                    {
                        "status": "failed",
                        "row_count": 0,
                    }
                ],
                "dashboard_keys": [],
            },
            "official credit sources failed",
        ),
        (
            "refresh_macro_data",
            "research.management.commands.refresh_macro_data.refresh_macro_official_data",
            {
                "runs": [
                    {
                        "status": "failed",
                        "row_count": 0,
                    }
                ],
                "dashboard_keys": [],
            },
            "official macro sources failed",
        ),
        (
            "refresh_github_data",
            "research.management.commands.refresh_github_data.refresh_github_sources",
            {"runs": [{}], "row_count": 0, "failed": 1, "partial": 0},
            "GitHub repositories failed",
        ),
        (
            "refresh_news_data",
            "research.management.commands.refresh_news_data.refresh_news_sources",
            {"runs": [{}], "row_count": 0, "failed": 1, "partial": 0},
            "official news feeds failed",
        ),
        (
            "refresh_berkshire_letters",
            "research.management.commands.refresh_berkshire_letters.refresh_berkshire_letters",
            {
                "runs": [
                    {
                        "source": "berkshire-hathaway",
                        "status": "partial",
                        "row_count": 39,
                        "error": "",
                        "metadata": {"first_year": 1977, "last_year": 2024},
                    }
                ],
                "row_count": 39,
                "failed": 0,
                "partial": 1,
            },
            "Berkshire letter index is incomplete",
        ),
        (
            "refresh_cftc_data",
            "research.management.commands.refresh_cftc_data.refresh_cftc_sources",
            {"runs": [{}], "row_count": 0, "failed": 0, "partial": 1},
            "CFTC TFF datasets failed or were incomplete",
        ),
    ],
)
def test_refresh_commands_raise_command_error_for_real_failures(
    command_name, target, summary, message
):
    with patch(target, return_value=summary):
        with pytest.raises(CommandError, match=message):
            call_command(command_name, stdout=StringIO(), stderr=StringIO())


@pytest.mark.parametrize(
    ("command_name", "target", "summary"),
    [
        (
            "refresh_macro_data",
            "research.management.commands.refresh_macro_data.refresh_macro_official_data",
            {
                "runs": [
                    {"status": "partial", "row_count": 0},
                    {"status": "partial", "row_count": 0},
                ],
                "dashboard_keys": [],
            },
        ),
        (
            "refresh_news_data",
            "research.management.commands.refresh_news_data.refresh_news_sources",
            {"runs": [{}, {}], "row_count": 0, "failed": 0, "partial": 2},
        ),
    ],
)
def test_optional_credential_partial_refreshes_keep_zero_exit_code(command_name, target, summary):
    with patch(target, return_value=summary):
        call_command(command_name, stdout=StringIO(), stderr=StringIO())


def test_refresh_h8_command_reports_source_dataset_rows_and_dashboard_keys():
    output = StringIO()
    summary = {
        "runs": [
            {
                "source": "federal-reserve",
                "dataset": "h8",
                "status": "success",
                "row_count": 1234,
                "error": "",
            }
        ],
        "dashboard_keys": ["reserves"],
        "stale_dashboard_keys": [],
    }
    with patch(
        "research.management.commands.refresh_h8_data.refresh_h8_data",
        return_value=summary,
    ):
        call_command("refresh_h8_data", stdout=output, stderr=StringIO())

    rendered = output.getvalue()
    assert "federal-reserve" in rendered
    assert "'dataset': 'h8'" in rendered
    assert "'row_count': 1234" in rendered
    assert "'dashboard_keys': ['reserves']" in rendered
    assert "H.8 refresh completed" in rendered


def test_refresh_h8_command_fails_when_atomic_reserves_publication_is_stale():
    summary = {
        "runs": [
            {
                "source": "federal-reserve",
                "dataset": "h8",
                "status": "success",
                "row_count": 1234,
                "error": "",
            }
        ],
        "dashboard_keys": [],
        "stale_dashboard_keys": ["reserves"],
    }
    with patch(
        "research.management.commands.refresh_h8_data.refresh_h8_data",
        return_value=summary,
    ):
        with pytest.raises(CommandError, match="atomic publication failed"):
            call_command(
                "refresh_h8_data", stdout=StringIO(), stderr=StringIO()
            )


def test_refresh_h41_command_fails_when_atomic_reserves_publication_is_stale():
    summary = {
        "runs": [
            {
                "source": "federal-reserve",
                "dataset": "h41",
                "status": "success",
                "row_count": 1234,
                "error": "",
            }
        ],
        "dashboard_keys": ["fed-balance-sheet"],
        "stale_dashboard_keys": ["reserves"],
    }
    with patch(
        "research.management.commands.refresh_h41_data.refresh_h41_data",
        return_value=summary,
    ):
        with pytest.raises(CommandError, match="atomic publication failed"):
            call_command(
                "refresh_h41_data", stdout=StringIO(), stderr=StringIO()
            )


def test_refresh_h41_command_fails_when_atomic_balance_sheet_publication_is_stale():
    summary = {
        "runs": [
            {
                "source": "federal-reserve",
                "dataset": "h41",
                "status": "success",
                "row_count": 1234,
                "error": "",
            }
        ],
        "dashboard_keys": ["reserves"],
        "stale_dashboard_keys": ["fed-balance-sheet"],
    }
    with patch(
        "research.management.commands.refresh_h41_data.refresh_h41_data",
        return_value=summary,
    ):
        with pytest.raises(
            CommandError, match="fed-balance-sheet v1 atomic publication failed"
        ):
            call_command(
                "refresh_h41_data", stdout=StringIO(), stderr=StringIO()
            )


def test_main_official_command_and_task_fail_when_balance_sheet_is_stale(
    monkeypatch,
):
    summary = {
        "runs": [
            {
                "source": "treasury-fiscal-data",
                "dataset": "daily-treasury-statement:tga",
                "status": "success",
                "row_count": 20,
                "error": "",
            }
        ],
        "dashboard_keys": [],
        "stale_dashboard_keys": ["fed-balance-sheet"],
    }
    with patch(
        "research.management.commands.refresh_official_data.refresh_official_data",
        return_value=summary,
    ):
        with pytest.raises(
            CommandError, match="fed-balance-sheet v1 atomic publication failed"
        ):
            call_command(
                "refresh_official_data", stdout=StringIO(), stderr=StringIO()
            )

    monkeypatch.setattr("research.tasks.refresh_official_data", lambda: summary)
    with pytest.raises(
        RuntimeError, match="fed-balance-sheet v1 atomic publication failed"
    ):
        refresh_official_sources.run()


def test_main_official_command_and_task_fail_when_subsurface_is_stale(
    monkeypatch,
):
    summary = {
        "runs": [
            {
                "source": "ny-fed-markets",
                "dataset": "reference-rate:sofr",
                "status": "success",
                "row_count": 65,
                "error": "",
            }
        ],
        "dashboard_keys": [],
        "stale_dashboard_keys": ["subsurface"],
    }
    with patch(
        "research.management.commands.refresh_official_data.refresh_official_data",
        return_value=summary,
    ):
        with pytest.raises(
            CommandError, match="subsurface v1 atomic publication failed"
        ):
            call_command(
                "refresh_official_data", stdout=StringIO(), stderr=StringIO()
            )

    monkeypatch.setattr("research.tasks.refresh_official_data", lambda: summary)
    with pytest.raises(
        RuntimeError, match="subsurface v1 atomic publication failed"
    ):
        refresh_official_sources.run()


def test_prates_command_and_task_fail_when_subsurface_is_stale(monkeypatch):
    summary = {
        "runs": [
            {
                "source": "federal-reserve",
                "dataset": "prates:iorb",
                "status": "success",
                "row_count": 65,
                "error": "",
            }
        ],
        "dashboard_keys": [],
        "stale_dashboard_keys": ["subsurface"],
    }
    with patch(
        "research.management.commands.refresh_prates_data.refresh_prates_data",
        return_value=summary,
    ):
        with pytest.raises(
            CommandError, match="subsurface v1 atomic publication failed"
        ):
            call_command(
                "refresh_prates_data", stdout=StringIO(), stderr=StringIO()
            )

    monkeypatch.setattr("research.tasks.refresh_prates_data", lambda: summary)
    with pytest.raises(
        RuntimeError, match="subsurface v1 atomic publication failed"
    ):
        refresh_prates_sources.run()
