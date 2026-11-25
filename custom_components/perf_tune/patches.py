"""The fixes themselves.

Every patch checks that the Home Assistant / library internals it relies on are
exactly as expected before touching anything. If a Home Assistant update changed
them, the patch is not applied: Home Assistant keeps working as usual and a
repair issue tells the user (see ``__init__.py``). Every patch can be reverted.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
import gc
import logging
import time
from typing import Any

from homeassistant.auth.permissions.const import POLICY_READ
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import HomeAssistant, callback

from .const import WINDOW_SECONDS

_LOGGER = logging.getLogger(__name__)


class PatchUnavailable(Exception):
    """The internals this patch needs are not there (or not as expected)."""


# ---------------------------------------------------------------------------
# 1. ws_batch: send state changes to browsers in batches
# ---------------------------------------------------------------------------


class _Batcher:
    """Collects the changes of one subscribe_entities subscription."""

    __slots__ = ("connection", "entity_filter", "entity_ids", "handle", "hass",
                 "interval", "msg_id_bytes", "pending", "unsub")

    def __init__(self, hass, connection, msg_id_bytes, entity_ids, entity_filter, interval):
        self.hass = hass
        self.connection = connection
        self.msg_id_bytes = msg_id_bytes
        self.entity_ids = entity_ids
        self.entity_filter = entity_filter
        self.interval = interval
        self.pending: set[str] = set()
        self.handle = None
        self.unsub = None

    @callback
    def on_event(self, event) -> None:
        entity_id = event.data["entity_id"]
        if (self.entity_ids and entity_id not in self.entity_ids) or (
            self.entity_filter and not self.entity_filter(entity_id)
        ):
            return
        user = self.connection.user
        perms = user.permissions
        if (
            not user.is_admin
            and not perms.access_all_entities(POLICY_READ)
            and not perms.check_entity(entity_id, POLICY_READ)
        ):
            return
        self.pending.add(entity_id)
        if self.handle is None:
            self.handle = self.hass.loop.call_later(self.interval, self.flush)

    @callback
    def flush(self) -> None:
        self.handle = None
        if not self.pending:
            return
        pending, self.pending = self.pending, set()
        added: list[bytes] = []
        removed: list[bytes] = []
        for entity_id in pending:
            state = self.hass.states.get(entity_id)
            if state is None:
                removed.append(b'"' + entity_id.encode() + b'"')
                continue
            try:
                added.append(state.as_compressed_state_json)
            except (ValueError, TypeError):
                continue
        parts = []
        if added:
            parts.append(b'"a":{' + b",".join(added) + b"}")
        if removed:
            parts.append(b'"r":[' + b",".join(removed) + b"]")
        if parts:
            self.connection.send_message(
                b'{"id":' + self.msg_id_bytes + b',"type":"event","event":{'
                + b",".join(parts) + b"}}"
            )

    @callback
    def cancel(self) -> None:
        if self.unsub:
            self.unsub()
            self.unsub = None
        if self.handle:
            self.handle.cancel()
            self.handle = None
        self.pending.clear()


class WsBatchPatch:
    """Replace the websocket command the frontend uses to follow all states."""

    def __init__(self, hass: HomeAssistant, interval: float) -> None:
        self.hass = hass
        self.interval = interval
        self._original = None

    def _internals(self):
        try:
            from homeassistant.components import websocket_api  # noqa: PLC0415
            from homeassistant.components.websocket_api import commands  # noqa: PLC0415
            from homeassistant.core import State  # noqa: PLC0415
            from homeassistant.helpers.entityfilter import (  # noqa: PLC0415
                convert_include_exclude_filter,
            )
        except ImportError as err:
            raise PatchUnavailable(f"import failed: {err}") from err
        original = getattr(commands, "handle_subscribe_entities", None)
        needed = {
            "commands.handle_subscribe_entities": original,
            "commands._async_get_allowed_states": getattr(commands, "_async_get_allowed_states", None),
            "commands._send_handle_entities_init_response": getattr(
                commands, "_send_handle_entities_init_response", None),
            "handle_subscribe_entities._ws_schema": getattr(original, "_ws_schema", None),
            "State.as_compressed_state_json": getattr(State, "as_compressed_state_json", None),
        }
        if missing := [name for name, value in needed.items() if value is None]:
            raise PatchUnavailable("missing: " + ", ".join(missing))
        if getattr(original, "_ws_command", None) != "subscribe_entities":
            raise PatchUnavailable("subscribe_entities command changed")
        handlers = self.hass.data.get(websocket_api.DOMAIN, {})
        if "subscribe_entities" not in handlers:
            raise PatchUnavailable("subscribe_entities is not registered")
        return websocket_api, commands, convert_include_exclude_filter, original

    def apply(self) -> None:
        websocket_api, commands, convert_filter, original = self._internals()
        interval = self.interval

        @callback
        def handle_subscribe_entities_batched(hass, connection, msg: dict[str, Any]) -> None:
            entity_ids = set(msg.get("entity_ids", [])) or None
            _filter = convert_filter(msg)
            entity_filter = None if _filter.empty_filter else _filter.get_filter()
            states = commands._async_get_allowed_states(hass, connection)  # noqa: SLF001
            msg_id = msg["id"]
            msg_id_bytes = str(msg_id).encode()
            batcher = _Batcher(hass, connection, msg_id_bytes, entity_ids, entity_filter, interval)
            batcher.unsub = hass.bus.async_listen(EVENT_STATE_CHANGED, batcher.on_event)
            connection.subscriptions[msg_id] = batcher.cancel
            connection.send_result(msg_id)
            serialized = []
            for state in states:
                if entity_ids and state.entity_id not in entity_ids:
                    continue
                if entity_filter and not entity_filter(state.entity_id):
                    continue
                try:
                    serialized.append(state.as_compressed_state_json)
                except (ValueError, TypeError):
                    continue
            commands._send_handle_entities_init_response(  # noqa: SLF001
                connection, msg_id_bytes, serialized
            )

        handle_subscribe_entities_batched._ws_schema = original._ws_schema  # noqa: SLF001
        handle_subscribe_entities_batched._ws_command = original._ws_command  # noqa: SLF001
        websocket_api.async_register_command(self.hass, handle_subscribe_entities_batched)
        self._original = original
        _LOGGER.info("perf_tune: subscribe_entities batched every %.2f s", interval)

    def revert(self) -> None:
        if self._original is None:
            return
        from homeassistant.components import websocket_api  # noqa: PLC0415

        websocket_api.async_register_command(self.hass, self._original)
        self._original = None


# ---------------------------------------------------------------------------
# 2. break_cycles: Event <-> Context reference cycle of state changes
# ---------------------------------------------------------------------------


class CycleBreakerPatch:
    """Clear context.origin_event after state_changed of the chosen entities.

    Home Assistant sets ``context.origin_event = event`` while ``event.context``
    is that context, so every state change leaves a reference cycle that only
    the expensive full garbage collection frees. ``origin_event`` is only read
    by the live logbook for "triggered by" information; sensors with a unit of
    measurement are not in the logbook anyway.
    """

    def __init__(self, hass: HomeAssistant, prefixes: tuple[str, ...]) -> None:
        self.hass = hass
        self.prefixes = prefixes
        self._unsub: Callable[[], None] | None = None

    def apply(self) -> None:
        from homeassistant.core import Context  # noqa: PLC0415

        if "origin_event" not in getattr(Context, "__slots__", ()):
            raise PatchUnavailable("Context.origin_event not found")
        if not self.prefixes:
            raise PatchUnavailable("no entity prefixes configured")
        prefixes = self.prefixes

        @callback
        def _break(event) -> None:
            if event.data["entity_id"].startswith(prefixes):
                ctx = event.context
                if ctx.origin_event is event:
                    ctx.origin_event = None

        self._unsub = self.hass.bus.async_listen(EVENT_STATE_CHANGED, _break)

    def revert(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None


# ---------------------------------------------------------------------------
# 3. paho-mqtt: MQTTMatcher.iter_match without a self-referencing closure
# ---------------------------------------------------------------------------


def _rec(node, lst, normal, i):
    if i == len(lst):
        if node._content is not None:  # noqa: SLF001
            yield node._content  # noqa: SLF001
    else:
        part = lst[i]
        children = node._children  # noqa: SLF001
        if part in children:
            yield from _rec(children[part], lst, normal, i + 1)
        if "+" in children and (normal or i > 0):
            yield from _rec(children["+"], lst, normal, i + 1)
    children = node._children  # noqa: SLF001
    if "#" in children and (normal or i > 0):
        content = children["#"]._content  # noqa: SLF001
        if content is not None:
            yield content


def _iter_match(self, topic):
    """Same result as paho's iter_match, but no closure per call."""
    return _rec(self._root, topic.split("/"), not topic.startswith("$"), 0)  # noqa: SLF001


_SELF_TEST_FILTERS = ("a/b/c", "a/+/c", "a/#", "#", "+/b/#", "$SYS/#", "+/+",
                      "homeassistant/+/+/config", "x/y/z/#", "x")
_SELF_TEST_TOPICS = ("a/b/c", "a/x/c", "a", "a/b", "$SYS/x", "$SYS", "q/b/z/z", "x",
                     "x/y/z", "homeassistant/sensor/abc/config", "", "/", "a//c")


class PahoPatch:
    """Replace paho's matcher only after checking it gives identical results."""

    def __init__(self) -> None:
        self._cls = None
        self._original = None

    def apply(self) -> None:
        try:
            from paho.mqtt.matcher import MQTTMatcher  # noqa: PLC0415
        except ImportError as err:
            raise PatchUnavailable(f"paho-mqtt not available: {err}") from err
        original = MQTTMatcher.__dict__.get("iter_match")
        if original is None:
            raise PatchUnavailable("MQTTMatcher.iter_match not found")
        if original is _iter_match:
            return
        matcher = MQTTMatcher()
        try:
            for f in _SELF_TEST_FILTERS:
                matcher[f] = f
            for topic in _SELF_TEST_TOPICS:
                if sorted(original(matcher, topic)) != sorted(_iter_match(matcher, topic)):
                    raise PatchUnavailable(f"self-test differs on topic {topic!r}")
        except PatchUnavailable:
            raise
        except Exception as err:  # noqa: BLE001
            raise PatchUnavailable(f"self-test failed: {err!r}") from err
        MQTTMatcher.iter_match = _iter_match
        self._cls, self._original = MQTTMatcher, original

    def revert(self) -> None:
        if self._cls is not None:
            self._cls.iter_match = self._original
            self._cls = self._original = None


# ---------------------------------------------------------------------------
# 4. gc.freeze() + monitoring
# ---------------------------------------------------------------------------


class GcMonitor:
    """Times every garbage collection and the event loop delay."""

    def __init__(self, hass: HomeAssistant, warn_ms: float) -> None:
        self.hass = hass
        self.warn_ms = warn_ms
        self._start = 0.0
        self._gen = 0
        self.events: deque[tuple[float, int, float]] = deque()  # (time, gen, ms)
        self.loop_delays: deque[tuple[float, float]] = deque()  # (time, ms)
        self._loop_handle = None
        self._expected = 0.0
        self.frozen_at: float | None = None

    # -- gc ---------------------------------------------------------------
    def _gc_callback(self, phase: str, info: dict) -> None:
        if phase == "start":
            self._start = time.perf_counter()
            self._gen = info.get("generation", 0)
            return
        ms = (time.perf_counter() - self._start) * 1000
        now = time.monotonic()
        self.events.append((now, self._gen, ms))
        while self.events and self.events[0][0] < now - WINDOW_SECONDS:
            self.events.popleft()
        if ms >= self.warn_ms:
            _LOGGER.warning(
                "perf_tune: garbage collection (generation %s) blocked Home Assistant "
                "for %.0f ms, %s objects collected", self._gen, ms, info.get("collected"))

    # -- loop delay -------------------------------------------------------
    def _tick(self) -> None:
        now = self.hass.loop.time()
        delay = max(0.0, (now - self._expected) * 1000)
        mono = time.monotonic()
        self.loop_delays.append((mono, delay))
        while self.loop_delays and self.loop_delays[0][0] < mono - WINDOW_SECONDS:
            self.loop_delays.popleft()
        self._expected = now + 0.25
        self._loop_handle = self.hass.loop.call_at(self._expected, self._tick)

    def start(self) -> None:
        gc.callbacks.append(self._gc_callback)
        self._expected = self.hass.loop.time() + 0.25
        self._loop_handle = self.hass.loop.call_at(self._expected, self._tick)

    def stop(self) -> None:
        if self._gc_callback in gc.callbacks:
            gc.callbacks.remove(self._gc_callback)
        if self._loop_handle:
            self._loop_handle.cancel()
            self._loop_handle = None

    # -- figures for the sensors -------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        recent = [e for e in self.events if e[0] >= now - WINDOW_SECONDS]
        total = sum(e[2] for e in recent)
        delays = [d for t, d in self.loop_delays if t >= now - WINDOW_SECONDS]
        return {
            "gc_share": round(100 * total / (WINDOW_SECONDS * 1000), 2),
            "gc_max_pause": round(max((e[2] for e in recent), default=0.0), 1),
            "gc_full_collections": sum(1 for e in recent if e[1] == 2),
            "loop_delay_max": round(max(delays, default=0.0), 1),
            "frozen_objects": gc.get_freeze_count(),
        }

    @staticmethod
    def freeze() -> dict[str, Any]:
        t0 = time.perf_counter()
        gc.collect()
        gc.freeze()
        return {"frozen_objects": gc.get_freeze_count(),
                "took_ms": round((time.perf_counter() - t0) * 1000)}

    @staticmethod
    def unfreeze() -> None:
        gc.unfreeze()


async def async_garbage_report(seconds: float) -> dict[str, Any]:
    """Which object types end up as cyclic garbage during ``seconds``."""
    import asyncio  # noqa: PLC0415
    from collections import Counter  # noqa: PLC0415

    gc.collect()
    gc.garbage.clear()
    gc.set_debug(gc.DEBUG_SAVEALL)
    try:
        await asyncio.sleep(seconds)
        gc.collect()
        garbage = list(gc.garbage)
    finally:
        gc.set_debug(0)
        gc.garbage.clear()
    types: Counter = Counter()
    origins: Counter = Counter()
    for obj in garbage:
        t = type(obj)
        types[f"{t.__module__}.{t.__qualname__}"] += 1
        code = getattr(obj, "f_code", None) or getattr(obj, "cr_code", None) or getattr(
            obj, "__code__", None)
        if code is not None:
            origins[code.co_filename] += 1
        elif t.__module__ != "builtins":
            origins[t.__module__] += 1
    count = len(garbage)
    del garbage
    return {"seconds": seconds, "objects": count,
            "top_types": types.most_common(20), "top_origins": origins.most_common(20)}
