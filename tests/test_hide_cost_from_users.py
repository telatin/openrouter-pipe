from __future__ import annotations

import logging

import pytest

from open_webui_openrouter_pipe import EventEmitterHandler, Pipe
from open_webui_openrouter_pipe.core.costs import redact_usage_cost

USAGE = {
    "input_tokens": 12,
    "output_tokens": 30,
    "total_tokens": 42,
    "cost": 0.0123,
    "cost_details": {"upstream_inference_cost": 0.01},
    "cache_discount": 0.001,
    "input_tokens_details": {"cached_tokens": 4},
}


def test_redaction_drops_spend_and_keeps_tokens():
    out = redact_usage_cost(dict(USAGE), Pipe.Valves())
    assert out is not None
    assert not {"cost", "cost_details", "cache_discount"} & set(out)
    assert out["total_tokens"] == 42
    assert out["input_tokens_details"] == {"cached_tokens": 4}


def test_redaction_leaves_the_input_untouched():
    original = dict(USAGE)
    redact_usage_cost(original, Pipe.Valves())
    assert original == USAGE


def test_admin_can_turn_redaction_off():
    valves = Pipe.Valves(HIDE_COST_FROM_USERS=False)
    assert redact_usage_cost(dict(USAGE), valves) == USAGE


def test_users_cannot_override_the_valve():
    assert "HIDE_COST_FROM_USERS" not in Pipe.UserValves.model_fields


@pytest.mark.asyncio
async def test_completion_event_carries_no_cost():
    handler = EventEmitterHandler(
        logger=logging.getLogger("t"), valves=Pipe.Valves(), pipe_instance=Pipe(), event_emitter=None
    )
    emitted: list[dict] = []

    async def capture(event):
        emitted.append(event)

    await handler._emit_completion(capture, content="hi", usage=dict(USAGE))
    usage = emitted[0]["data"]["usage"]
    assert "cost" not in usage and "cost_details" not in usage
    assert usage["total_tokens"] == 42


def test_status_line_names_no_cost_by_default(pipe_instance):
    description = pipe_instance._ensure_error_formatter()._format_final_status_description(
        elapsed=1.0, total_usage=dict(USAGE), valves=pipe_instance.valves
    )
    assert "Cost" not in description and "0.0123" not in description
    assert "Total tokens: 42" in description
