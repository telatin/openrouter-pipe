"""Cost governor: per-user spend ledger plus group-based monthly limits.

Hooks:
  on_request              resolve the user's limit; over it, switch to the fallback model or refuse
  on_generation_complete  append one ledger row with the real cost (never shown to the user)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, ClassVar

from pydantic import Field

from ...models.registry import sanitize_model_id
from ..base import PluginBase, PluginContext
from ..registry import PluginRegistry
from .ledger import CostLedger
from .policy import (
    governor_month_window,
    governor_parse_group_limits,
    governor_resolve_limit,
)

logger = logging.getLogger(__name__)

_CG_SPEND_TTL_S = 5.0
_CG_GROUPS_TTL_S = 60.0
_CG_PENDING_MAX = 2000
_CG_PENDING_TTL_S = 6 * 3600.0
_CG_DEFAULT_MESSAGE = (
    "Your usage allowance for this month has been used. It renews on the 1st. "
    "Please choose a local model, or contact the administrator."
)


async def governor_load_group_names(user_id: str) -> list[str]:
    """Open WebUI group names for ``user_id``."""
    from open_webui.models.groups import Groups  # type: ignore[import-not-found]

    result = Groups.get_groups_by_member_id(user_id)
    if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
        result = await result
    return [str(getattr(group, "name", "") or "") for group in (result or []) if getattr(group, "name", "")]


def _cg_usage_int(usage: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
    return None


def _cg_detail(usage: dict[str, Any], container: str, key: str) -> int | None:
    block = usage.get(container)
    if isinstance(block, dict):
        value = block.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    return None


@PluginRegistry.register
class CostGovernorPlugin(PluginBase):
    plugin_id = "cost-governor"
    plugin_name = "Cost Governor"
    plugin_version = "1.0.0"
    hooks: ClassVar[dict[str, int]] = {
        "on_request": 90,
        "on_generation_complete": 90,
    }

    plugin_valves: ClassVar[dict[str, tuple]] = {
        "GOVERNOR_ENABLE": (bool, Field(
            default=False,
            title="Enable cost governor",
            description="Record every request's real cost per user in a ledger and enforce monthly limits. "
                        "Requires the plugin system to be enabled. Users never see these figures.",
        )),
        "GOVERNOR_DB_URL": (str, Field(
            default="",
            title="Ledger database URL",
            description="SQLAlchemy URL for the ledger, e.g. mysql+pymysql://user:pass@host/db. "
                        "Leave blank to use Open WebUI's own database.",
        )),
        "GOVERNOR_DEFAULT_MONTHLY_LIMIT_USD": (float, Field(
            default=2.0,
            title="Default monthly limit (USD)",
            description="Applies to users in none of the groups listed below. A negative value means unlimited.",
        )),
        "GOVERNOR_GROUP_LIMITS": (str, Field(
            default="quota-base=5, quota-medium=20, quota-pro=100",
            title="Monthly limit per Open WebUI group (USD)",
            description="Comma-separated name=amount pairs. Use 'unlimited' for no cap. "
                        "A user in several groups gets the most generous limit.",
        )),
        "GOVERNOR_EXEMPT_ADMINS": (bool, Field(
            default=True,
            title="Exempt administrators from limits",
            description="Admin requests are still recorded in the ledger.",
        )),
        "GOVERNOR_TIMEZONE": (str, Field(
            default="UTC",
            title="Month boundary time zone",
            description="IANA time zone name deciding when the calendar month rolls over, e.g. Europe/London.",
        )),
        "GOVERNOR_OVER_LIMIT_MODEL": (str, Field(
            default="",
            title="Free fallback model once over the limit",
            description="OpenRouter model id (for example a ':free' model) served instead once a user is over their "
                        "limit. It must be allowed by the model allowlist. Blank refuses chat requests with the "
                        "message below.",
        )),
        "GOVERNOR_OVER_LIMIT_MESSAGE": (str, Field(
            default=_CG_DEFAULT_MESSAGE,
            title="Over-limit message",
            description="Shown to the user when a request is refused. Do not include figures.",
        )),
    }

    def on_init(self, ctx: PluginContext, **kwargs: Any) -> None:
        self.ctx = ctx
        self._ledger: CostLedger | None = None
        self._ledger_key: str | None = None
        self._owns_engine = False
        self._spend_cache: dict[str, tuple[float, float]] = {}
        self._group_cache: dict[str, tuple[float, list[str]]] = {}
        self._pending: dict[str, dict[str, Any]] = {}
        self._build_lock = asyncio.Lock()

    def _valve(self, name: str, default: Any) -> Any:
        return getattr(self.ctx.valves, name, default)

    def _enabled(self, valves: Any = None) -> bool:
        source = valves if valves is not None else self.ctx.valves
        return bool(getattr(source, "GOVERNOR_ENABLE", False))

    def _make_ledger(self, url: str) -> CostLedger | None:
        if url:
            from sqlalchemy import create_engine

            self._owns_engine = True
            return CostLedger(create_engine(url, pool_pre_ping=True))
        store = self.ctx.artifact_store
        engine, schema = store._ensure_db_handle()
        if engine is None:
            return None
        self._owns_engine = False
        return CostLedger(engine, schema=schema if isinstance(schema, str) and schema.strip() else None)

    async def _get_ledger(self) -> CostLedger | None:
        url = str(self._valve("GOVERNOR_DB_URL", "") or "").strip()
        if self._ledger is not None and self._ledger_key == url:
            return self._ledger
        async with self._build_lock:
            if self._ledger is not None and self._ledger_key == url:
                return self._ledger
            previous, owned = self._ledger, self._owns_engine
            ledger = await asyncio.to_thread(self._make_ledger, url)
            if ledger is not None:
                await asyncio.to_thread(ledger.ensure)
            if previous is not None and owned:
                await asyncio.to_thread(previous.engine.dispose)
            self._ledger, self._ledger_key = ledger, url
            return ledger

    async def _group_names(self, user_id: str) -> list[str]:
        cached = self._group_cache.get(user_id)
        now = time.monotonic()
        if cached and cached[0] > now:
            return cached[1]
        names = await governor_load_group_names(user_id)
        self._group_cache[user_id] = (now + _CG_GROUPS_TTL_S, names)
        return names

    async def _month_spend(self, ledger: CostLedger, user_id: str, window: tuple[int, int]) -> float:
        key = f"{user_id}:{window[0]}"
        cached = self._spend_cache.get(key)
        now = time.monotonic()
        if cached and cached[0] > now:
            return cached[1]
        value = await asyncio.to_thread(ledger.spend, user_id, window[0], window[1])
        self._spend_cache[key] = (now + _CG_SPEND_TTL_S, value)
        return value

    def _remember(self, request_id: str, info: dict[str, Any]) -> None:
        if not request_id:
            return
        if len(self._pending) >= _CG_PENDING_MAX:
            cutoff = time.time() - _CG_PENDING_TTL_S
            for key in [k for k, v in self._pending.items() if v.get("seen", 0) < cutoff]:
                self._pending.pop(key, None)
            while len(self._pending) >= _CG_PENDING_MAX:
                self._pending.pop(next(iter(self._pending)), None)
        info["seen"] = time.time()
        self._pending[request_id] = info

    def _fallback_model(self, pipe_id: str) -> str:
        raw = str(self._valve("GOVERNOR_OVER_LIMIT_MODEL", "") or "").strip()
        if not raw:
            return ""
        prefix = f"{pipe_id}."
        return raw if raw.startswith(prefix) else f"{pipe_id}.{sanitize_model_id(raw)}"

    async def on_request(
        self,
        body: dict[str, Any],
        user: dict[str, Any],
        metadata: dict[str, Any],
        event_emitter: Any,
        task: Any,
        **kwargs: Any,
    ) -> dict[str, Any] | str | None:
        valves = kwargs.get("valves")
        if not self._enabled(valves):
            return None
        request_id = str(kwargs.get("request_id") or "")
        user_id = str((user or {}).get("id") or (metadata or {}).get("user_id") or "")
        if not user_id:
            return None
        info: dict[str, Any] = {
            "user_id": user_id,
            "user_name": (user or {}).get("name"),
            "user_email": (user or {}).get("email"),
            "requested_model": str(body.get("model") or ""),
            "degraded": False,
            "groups": "",
        }
        try:
            groups = await self._group_names(user_id)
            info["groups"] = ",".join(groups)
            limits, problems = governor_parse_group_limits(str(getattr(valves, "GOVERNOR_GROUP_LIMITS", "") or ""))
            if problems:
                logger.warning("cost governor: unreadable GOVERNOR_GROUP_LIMITS entries ignored: %s", problems)
            limit = governor_resolve_limit(
                groups,
                limits,
                float(getattr(valves, "GOVERNOR_DEFAULT_MONTHLY_LIMIT_USD", 2.0)),
                is_admin=str((user or {}).get("role") or "") == "admin",
                exempt_admins=bool(getattr(valves, "GOVERNOR_EXEMPT_ADMINS", True)),
            )
            self._remember(request_id, info)
            if limit is None or task:
                return None
            ledger = await self._get_ledger()
            if ledger is None:
                logger.warning("cost governor: no database available; limits are not enforced")
                return None
            window = governor_month_window(time.time(), str(getattr(valves, "GOVERNOR_TIMEZONE", "UTC")))
            if await self._month_spend(ledger, user_id, window) < limit:
                return None
        except Exception:
            logger.warning("cost governor: limit check failed; request allowed", exc_info=True)
            return None

        fallback = self._fallback_model(self.ctx.pipe_id)
        if fallback:
            body["model"] = fallback
            info["degraded"] = True
            return None
        message = str(getattr(valves, "GOVERNOR_OVER_LIMIT_MESSAGE", "") or _CG_DEFAULT_MESSAGE)
        return self.ctx.build_response(model=str(body.get("model") or "pipe"), content=message)

    async def on_generation_complete(
        self,
        usage: dict[str, Any] | None,
        status: str,
        **kwargs: Any,
    ) -> None:
        if not self._enabled():
            return
        request_id = str(kwargs.get("request_id") or "")
        raw_metadata = kwargs.get("metadata")
        metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
        info = self._pending.pop(request_id, None) or {}
        user_id = str(info.get("user_id") or metadata.get("user_id") or "")
        if not user_id or not request_id:
            return
        usage = usage if isinstance(usage, dict) else {}
        task = kwargs.get("task")
        model_block = metadata.get("model")
        model_id = str(model_block.get("id") or "") if isinstance(model_block, dict) else ""
        cost_raw = usage.get("cost")
        cost = float(cost_raw) if isinstance(cost_raw, (int, float)) and not isinstance(cost_raw, bool) else 0.0
        row = {
            "ts": int(time.time()),
            "request_id": request_id,
            "user_id": user_id,
            "user_name": info.get("user_name"),
            "user_email": info.get("user_email"),
            "user_groups": info.get("groups"),
            "pipe_id": self.ctx.pipe_id,
            "kind": "task" if task else "chat",
            "model_id": model_id or info.get("requested_model"),
            "requested_model": info.get("requested_model"),
            "status": status,
            "degraded": info.get("degraded", False),
            "tokens_in": _cg_usage_int(usage, "input_tokens", "prompt_tokens"),
            "tokens_out": _cg_usage_int(usage, "output_tokens", "completion_tokens"),
            "tokens_reasoning": _cg_detail(usage, "output_tokens_details", "reasoning_tokens"),
            "tokens_cached": _cg_detail(usage, "input_tokens_details", "cached_tokens"),
            "cost": cost,
            "chat_id": metadata.get("chat_id"),
            "message_id": metadata.get("message_id"),
        }
        try:
            ledger = await self._get_ledger()
            if ledger is None:
                logger.error("cost governor: no database; dropped ledger row for request %s", request_id)
                return
            await asyncio.to_thread(ledger.record, row)
            window = governor_month_window(time.time(), str(self._valve("GOVERNOR_TIMEZONE", "UTC")))
            self._spend_cache.pop(f"{user_id}:{window[0]}", None)
        except Exception:
            logger.exception("cost governor: failed to record ledger row for request %s", request_id)

    def on_shutdown(self, **kwargs: Any) -> Any:
        ledger, owned = getattr(self, "_ledger", None), getattr(self, "_owns_engine", False)
        self._ledger = None
        if ledger is not None and owned:
            try:
                ledger.engine.dispose()
            except Exception:
                logger.debug("cost governor: engine dispose failed", exc_info=True)
        return None
