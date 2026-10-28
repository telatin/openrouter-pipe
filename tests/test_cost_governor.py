"""Cost governor: group limits, month windows, ledger on SQLite, and the two hooks."""

from __future__ import annotations

import datetime
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest

pytest.importorskip("open_webui_openrouter_pipe.plugins.cost_governor")

from open_webui_openrouter_pipe import Pipe
from open_webui_openrouter_pipe.plugins.base import PluginContext
from open_webui_openrouter_pipe.plugins.cost_governor import plugin as governor_plugin
from open_webui_openrouter_pipe.plugins.cost_governor.ledger import CostLedger
from open_webui_openrouter_pipe.plugins.cost_governor.plugin import CostGovernorPlugin
from open_webui_openrouter_pipe.plugins.cost_governor.policy import (
    governor_month_window,
    governor_parse_group_limits,
    governor_resolve_limit,
)

LIMITS = "quota-base=5, quota-medium=20, Quota-Pro=100, staff=unlimited"


def test_parse_group_limits_reads_pairs_and_reports_junk():
    limits, problems = governor_parse_group_limits(LIMITS + ", broken, x=abc, y=-3")
    assert limits == {"quota-base": 5.0, "quota-medium": 20.0, "quota-pro": 100.0, "staff": None}
    assert problems == ["broken", "x=abc", "y=-3"]


@pytest.mark.parametrize(
    ("groups", "default", "admin", "expected"),
    [
        ([], 2.0, False, 2.0),
        ([], -1.0, False, None),
        (["quota-base"], 2.0, False, 5.0),
        (["quota-base", "QUOTA-PRO"], 2.0, False, 100.0),
        (["quota-base", "staff"], 2.0, False, None),
        (["unrelated"], 2.0, False, 2.0),
        ([], 2.0, True, None),
    ],
)
def test_resolve_limit(groups, default, admin, expected):
    limits, _ = governor_parse_group_limits(LIMITS)
    assert governor_resolve_limit(groups, limits, default, is_admin=admin, exempt_admins=True) == expected


def test_admin_not_exempt_when_valve_off():
    assert governor_resolve_limit([], {}, 2.0, is_admin=True, exempt_admins=False) == 2.0


def test_month_window_is_calendar_month_in_zone():
    now = datetime.datetime(2026, 12, 31, 23, 30, tzinfo=datetime.UTC).timestamp()
    start, end = governor_month_window(now, "UTC")
    assert datetime.datetime.fromtimestamp(start, datetime.UTC) == datetime.datetime(2026, 12, 1, tzinfo=datetime.UTC)
    assert datetime.datetime.fromtimestamp(end, datetime.UTC) == datetime.datetime(2027, 1, 1, tzinfo=datetime.UTC)
    start_tokyo, _ = governor_month_window(now, "Asia/Tokyo")
    assert datetime.datetime.fromtimestamp(start_tokyo, datetime.UTC) == datetime.datetime(2026, 12, 31, 15, tzinfo=datetime.UTC)
    assert governor_month_window(now, "Not/AZone") == (start, end)


@pytest.fixture
def ledger(tmp_path):
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    yield CostLedger(engine)
    engine.dispose()


def _row(request_id: str, user: str, cost: float, ts: int, **extra: Any) -> dict[str, Any]:
    return {"request_id": request_id, "user_id": user, "cost": cost, "ts": ts, **extra}


def test_ledger_sums_per_user_and_month(ledger):
    assert ledger.record(_row("r1", "u1", 0.5, 100))
    assert ledger.record(_row("r2", "u1", 0.25, 150))
    assert ledger.record(_row("r3", "u1", 9.0, 500))
    assert ledger.record(_row("r4", "u2", 1.0, 120))
    assert ledger.spend("u1", 0, 200) == pytest.approx(0.75)
    assert ledger.spend("nobody", 0, 200) == 0.0
    by_user = ledger.spend_by_user(0, 200)
    assert [(r["user_id"], round(r["cost"], 2)) for r in by_user] == [("u2", 1.0), ("u1", 0.75)]


def test_ledger_ignores_a_duplicate_request(ledger):
    assert ledger.record(_row("r1", "u1", 0.5, 100))
    assert not ledger.record(_row("r1", "u1", 0.5, 100))
    assert ledger.spend("u1", 0, 200) == pytest.approx(0.5)


def test_ledger_truncates_overlong_text(ledger):
    ledger.record(_row("r1", "u1", 0.1, 100, user_name="n" * 500))
    assert len(ledger.rows(0, 200)[0]["user_name"]) == 128


@pytest.fixture
def governor(tmp_path, monkeypatch):
    db = f"sqlite:///{tmp_path / 'gov.db'}"
    valves = cast(Any, Pipe.Valves)(
        GOVERNOR_ENABLE=True,
        GOVERNOR_DB_URL=db,
        GOVERNOR_DEFAULT_MONTHLY_LIMIT_USD=1.0,
        GOVERNOR_GROUP_LIMITS="quota-base=5",
    )
    pipe = SimpleNamespace(valves=valves, id="owui_pipe", _artifact_store=None)
    plugin = CostGovernorPlugin()
    plugin.on_init(PluginContext(pipe=pipe, logger=governor_plugin.logger))  # type: ignore[arg-type]
    groups: dict[str, list[str]] = {}

    async def fake_groups(user_id: str) -> list[str]:
        return groups.get(user_id, [])

    monkeypatch.setattr(governor_plugin, "governor_load_group_names", fake_groups)
    yield SimpleNamespace(plugin=plugin, valves=valves, groups=groups)
    plugin.on_shutdown()


async def _chat(g, user_id="u1", model="owui_pipe.openai.gpt-4o", role="user", rid="req-1", task=None):
    body = {"model": model, "stream": True}
    result = await g.plugin.on_request(
        body, {"id": user_id, "name": "N", "email": "n@x", "role": role}, {"user_id": user_id},
        None, task, valves=g.valves, request_id=rid,
    )
    return body, result


async def _complete(g, rid, cost, user_id="u1", status="ok"):
    await g.plugin.on_generation_complete(
        {"cost": cost, "input_tokens": 10, "output_tokens": 5,
         "output_tokens_details": {"reasoning_tokens": 2}},
        status, request_id=rid, task=None,
        metadata={"user_id": user_id, "chat_id": "c1", "message_id": "m1",
                  "model": {"id": "owui_pipe.openai.gpt-4o"}},
    )


@pytest.mark.asyncio
async def test_request_under_limit_proceeds_untouched(governor):
    body, result = await _chat(governor)
    assert result is None and body["model"] == "owui_pipe.openai.gpt-4o"


@pytest.mark.asyncio
async def test_completion_is_recorded_with_cost_and_user(governor):
    await _chat(governor, rid="r1")
    await _complete(governor, "r1", 0.4)
    ledger = await governor.plugin._get_ledger()
    rows = ledger.rows(0, 2**40)
    assert len(rows) == 1
    row = rows[0]
    assert (row["user_id"], row["cost"], row["tokens_in"], row["tokens_reasoning"]) == ("u1", 0.4, 10, 2)
    assert (row["user_email"], row["chat_id"], row["kind"], row["pipe_id"]) == ("n@x", "c1", "chat", "owui_pipe")


@pytest.mark.asyncio
async def test_over_limit_is_refused_without_figures(governor):
    await _chat(governor, rid="r1")
    await _complete(governor, "r1", 1.5)
    governor.plugin._spend_cache.clear()
    _, result = await _chat(governor, rid="r2")
    assert isinstance(result, dict)
    text = result["choices"][0]["message"]["content"]
    assert "allowance" in text and "1.5" not in text and "$" not in text


@pytest.mark.asyncio
async def test_group_limit_raises_the_ceiling(governor):
    governor.groups["u1"] = ["quota-base"]
    await _chat(governor, rid="r1")
    await _complete(governor, "r1", 1.5)
    governor.plugin._spend_cache.clear()
    _, result = await _chat(governor, rid="r2")
    assert result is None


@pytest.mark.asyncio
async def test_over_limit_switches_to_the_fallback_model_and_flags_the_row(governor):
    governor.valves.GOVERNOR_OVER_LIMIT_MODEL = "meta-llama/llama-3.3-70b-instruct:free"
    await _chat(governor, rid="r1")
    await _complete(governor, "r1", 2.0)
    governor.plugin._spend_cache.clear()
    body, result = await _chat(governor, rid="r2")
    assert result is None
    assert body["model"] == "owui_pipe.meta-llama.llama-3.3-70b-instruct:free"
    await _complete(governor, "r2", 0.0)
    rows = (await governor.plugin._get_ledger()).rows(0, 2**40)
    assert [r["degraded"] for r in rows] == [0, 1]


@pytest.mark.asyncio
async def test_admins_and_tasks_are_never_blocked(governor):
    await _chat(governor, rid="r1", user_id="adm", role="admin")
    await _complete(governor, "r1", 50.0, user_id="adm")
    governor.plugin._spend_cache.clear()
    assert (await _chat(governor, rid="r2", user_id="adm", role="admin"))[1] is None
    await _chat(governor, rid="r3")
    await _complete(governor, "r3", 5.0)
    governor.plugin._spend_cache.clear()
    assert (await _chat(governor, rid="r4", task="title_generation"))[1] is None


@pytest.mark.asyncio
async def test_disabled_governor_records_and_blocks_nothing(governor):
    governor.valves.GOVERNOR_ENABLE = False
    assert (await _chat(governor))[1] is None
    await _complete(governor, "r1", 99.0)
    assert governor.plugin._ledger is None


@pytest.mark.asyncio
async def test_database_failure_fails_open(governor, monkeypatch):
    async def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(governor.plugin, "_get_ledger", boom)
    assert (await _chat(governor))[1] is None
    await _complete(governor, "r1", 1.0)


def test_users_cannot_see_governor_valves():
    assert not [k for k in Pipe.UserValves.model_fields if k.startswith("GOVERNOR_")]
    assert "GOVERNOR_GROUP_LIMITS" in Pipe.Valves.model_fields


@pytest.mark.asyncio
async def test_blank_db_url_uses_the_open_webui_engine(tmp_path):
    from sqlalchemy import create_engine, inspect

    engine = create_engine(f"sqlite:///{tmp_path / 'owui.db'}")
    store = SimpleNamespace(_ensure_db_handle=lambda: (engine, None))
    valves = cast(Any, Pipe.Valves)(GOVERNOR_ENABLE=True)
    pipe = SimpleNamespace(valves=valves, id="owui_pipe", _artifact_store=store)
    plugin = CostGovernorPlugin()
    plugin.on_init(PluginContext(pipe=pipe, logger=governor_plugin.logger))  # type: ignore[arg-type]
    ledger = await plugin._get_ledger()
    assert ledger is not None and ledger.engine is engine
    assert "openrouter_cost_ledger" in inspect(engine).get_table_names()
    plugin.on_shutdown()
    assert plugin._owns_engine is False
    engine.dispose()
