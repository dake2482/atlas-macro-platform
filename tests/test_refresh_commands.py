from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from research.tasks import (
    refresh_credit_official_sources,
    refresh_h8_sources,
    refresh_h10_sources,
    refresh_h41_sources,
    refresh_official_sources,
    refresh_prates_sources,
    refresh_treasury_curve_sources,
)


def test_credit_command_and_task_fail_only_when_no_strict_publication_is_available(
    monkeypatch,
):
    unavailable = {
        "runs": [
            {
                "source": "us-treasury-hqm",
                "dataset": "monthly-average-par-yields",
                "status": "success",
                "row_count": 480,
                "error": "",
            }
        ],
        "dashboard_keys": [],
        "stale_dashboard_keys": ["credit", "credit-stress"],
        "credit_refresh_id": "fixture-cycle",
    }
    with patch(
        "research.management.commands.refresh_credit_data.refresh_credit_official_data",
        return_value=unavailable,
    ):
        with pytest.raises(CommandError, match="official credit sources failed"):
            call_command("refresh_credit_data", stdout=StringIO(), stderr=StringIO())

    monkeypatch.setattr("research.tasks.refresh_credit_official_data", lambda: unavailable)
    with pytest.raises(RuntimeError, match="Credit Official v1"):
        refresh_credit_official_sources.run()

    legally_retained = {
        **unavailable,
        "runs": [{**unavailable["runs"][0], "status": "failed", "row_count": 0}],
        "stale_dashboard_keys": [],
    }
    with patch(
        "research.management.commands.refresh_credit_data.refresh_credit_official_data",
        return_value=legally_retained,
    ):
        call_command("refresh_credit_data", stdout=StringIO(), stderr=StringIO())


def test_treasury_daily_task_refreshes_only_current_year_and_fails_loudly(
    monkeypatch,
):
    calls = []

    def refresh(**kwargs):
        calls.append(kwargs)
        return {
            "runs": [],
            "dashboard_keys": [],
            "stale_dashboard_keys": ["yield-curve"],
        }

    monkeypatch.setattr("research.tasks.refresh_treasury_curve_data", refresh)
    with pytest.raises(RuntimeError, match="Treasury curve v2"):
        refresh_treasury_curve_sources.run()
    assert len(calls) == 1
    assert calls[0]["start_year"] == calls[0]["end_year"]

    monkeypatch.setattr(
        "research.tasks.refresh_treasury_curve_data",
        lambda **kwargs: {
            "runs": [],
            "dashboard_keys": ["yield-curve", "real-rates"],
            "stale_dashboard_keys": [],
            "requested": kwargs,
        },
    )
    result = refresh_treasury_curve_sources.run()
    assert result["stale_dashboard_keys"] == []


@pytest.mark.parametrize(
    ("command_name", "command_target", "task", "task_target"),
    [
        (
            "refresh_official_data",
            "research.management.commands.refresh_official_data.refresh_official_data",
            refresh_official_sources,
            "research.tasks.refresh_official_data",
        ),
        (
            "refresh_h41_data",
            "research.management.commands.refresh_h41_data.refresh_h41_data",
            refresh_h41_sources,
            "research.tasks.refresh_h41_data",
        ),
        (
            "refresh_h8_data",
            "research.management.commands.refresh_h8_data.refresh_h8_data",
            refresh_h8_sources,
            "research.tasks.refresh_h8_data",
        ),
        (
            "refresh_prates_data",
            "research.management.commands.refresh_prates_data.refresh_prates_data",
            refresh_prates_sources,
            "research.tasks.refresh_prates_data",
        ),
        (
            "refresh_h10_data",
            "research.management.commands.refresh_h10_data.refresh_h10_data",
            refresh_h10_sources,
            "research.tasks.refresh_h10_data",
        ),
    ],
)
def test_all_required_commands_and_tasks_fail_loudly_for_transmission_chain(
    monkeypatch,
    command_name,
    command_target,
    task,
    task_target,
):
    summary = {
        "runs": [
            {
                "source": "official-fixture",
                "dataset": "fixture",
                "status": "success",
                "row_count": 1,
                "error": "",
            }
        ],
        "dashboard_keys": [],
        "stale_dashboard_keys": ["transmission-chain"],
    }
    with patch(command_target, return_value=summary):
        with pytest.raises(CommandError, match="transmission-chain v1"):
            call_command(
                command_name,
                stdout=StringIO(),
                stderr=StringIO(),
            )

    monkeypatch.setattr(task_target, lambda: summary)
    with pytest.raises(RuntimeError, match="transmission-chain v1"):
        task.run()


@pytest.mark.parametrize(
    ("command_name", "command_target", "task", "task_target"),
    [
        (
            "refresh_official_data",
            "research.management.commands.refresh_official_data.refresh_official_data",
            refresh_official_sources,
            "research.tasks.refresh_official_data",
        ),
        (
            "refresh_h10_data",
            "research.management.commands.refresh_h10_data.refresh_h10_data",
            refresh_h10_sources,
            "research.tasks.refresh_h10_data",
        ),
    ],
)
def test_official_and_h10_commands_and_tasks_fail_loudly_for_assets_fx(
    monkeypatch,
    command_name,
    command_target,
    task,
    task_target,
):
    summary = {
        "runs": [
            {
                "source": "federal-reserve",
                "dataset": "h10",
                "status": "success",
                "row_count": 1200,
                "error": "",
            }
        ],
        "dashboard_keys": [],
        "stale_dashboard_keys": ["assets-fx"],
    }
    with patch(command_target, return_value=summary):
        with pytest.raises(CommandError, match="assets-fx v1"):
            call_command(command_name, stdout=StringIO(), stderr=StringIO())

    monkeypatch.setattr(task_target, lambda: summary)
    with pytest.raises(RuntimeError, match="assets-fx v1"):
        task.run()


@pytest.mark.parametrize(
    ("command_name", "command_target", "task", "task_target"),
    [
        (
            "refresh_official_data",
            "research.management.commands.refresh_official_data.refresh_official_data",
            refresh_official_sources,
            "research.tasks.refresh_official_data",
        ),
        (
            "refresh_h10_data",
            "research.management.commands.refresh_h10_data.refresh_h10_data",
            refresh_h10_sources,
            "research.tasks.refresh_h10_data",
        ),
    ],
)
def test_official_and_h10_commands_and_tasks_fail_loudly_for_fx_vol(
    monkeypatch,
    command_name,
    command_target,
    task,
    task_target,
):
    summary = {
        "runs": [
            {
                "source": "federal-reserve",
                "dataset": "h10",
                "status": "success",
                "row_count": 1200,
                "error": "",
            }
        ],
        "dashboard_keys": [],
        "stale_dashboard_keys": ["fx-vol"],
    }
    with patch(command_target, return_value=summary):
        with pytest.raises(CommandError, match="fx-vol v1"):
            call_command(command_name, stdout=StringIO(), stderr=StringIO())

    monkeypatch.setattr(task_target, lambda: summary)
    with pytest.raises(RuntimeError, match="fx-vol v1"):
        task.run()


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


def test_main_official_command_and_task_fail_when_operations_is_stale(
    monkeypatch,
):
    summary = {
        "runs": [
            {
                "source": "ny-fed-markets",
                "dataset": "treasury:purchases",
                "status": "success",
                "row_count": 35,
                "error": "",
            }
        ],
        "dashboard_keys": [],
        "stale_dashboard_keys": ["operations"],
    }
    with patch(
        "research.management.commands.refresh_official_data.refresh_official_data",
        return_value=summary,
    ):
        with pytest.raises(
            CommandError, match="operations v1 atomic publication failed"
        ):
            call_command(
                "refresh_official_data", stdout=StringIO(), stderr=StringIO()
            )

    monkeypatch.setattr("research.tasks.refresh_official_data", lambda: summary)
    with pytest.raises(
        RuntimeError, match="operations v1 atomic publication failed"
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
