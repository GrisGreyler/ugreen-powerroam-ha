"""Optional watchdogs that hold selected station states.

The station sometimes reboots on its own and comes back with the inverter
off. For every enabled target this controller watches telemetry and, whenever
the reported value differs from the wanted one, sends the command until the
station confirms it. It does not matter who changed the value (reboot, UGREEN
app, physical button, HA switch): while a watchdog is on, the value is forced
back. Turn the watchdog off to switch an output off deliberately.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# telemetry key -> (switch name, wanted raw value)
KEEP_TARGETS: dict[str, tuple[str, int]] = {
    "switch_ac": ("Keep AC On", 1),
    "usb_sw": ("Keep USB On", 1),
    "work_mode": ("Keep Always On Mode", 3),
}

SETTLE_DELAY = 5  # let a rebooted unit finish publishing its state first
POLL_STEP = 5
CONFIRM_TIMEOUT = 45  # telemetry ~15 s plus the hub's 15 s change debounce
MAX_ATTEMPTS = 5


class KeepState:
    def __init__(self, hass: HomeAssistant, hub, api) -> None:
        self._hass = hass
        self._hub = hub
        self._api = api
        self._enabled: set[str] = set()
        self._tasks: dict[str, asyncio.Task] = {}
        self._gave_up: set[str] = set()
        self._listeners: list[Callable[[], None]] = []
        self._unsub: Callable[[], None] | None = None

    def start(self) -> None:
        self._unsub = self._hub.add_listener(self._on_update)

    async def stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None
        for key in list(self._tasks):
            self._cancel(key)

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    def is_enabled(self, key: str) -> bool:
        return key in self._enabled

    def set_enabled(self, key: str, value: bool) -> None:
        if value == (key in self._enabled):
            return
        self._gave_up.discard(key)
        if value:
            self._enabled.add(key)
        else:
            self._enabled.discard(key)
            self._cancel(key)
        for cb in list(self._listeners):
            cb()
        if value:
            self._check(key)

    def _mismatch(self, key: str) -> bool:
        if not self._hub.available:
            return False
        current = self._hub.data.get(key)
        return current is not None and current != KEEP_TARGETS[key][1]

    def _cancel(self, key: str) -> None:
        task = self._tasks.pop(key, None)
        if task and not task.done():
            task.cancel()

    def _on_update(self) -> None:
        for key in KEEP_TARGETS:
            self._check(key)

    def _check(self, key: str) -> None:
        if not self._mismatch(key):
            self._gave_up.discard(key)
            return
        if key not in self._enabled or key in self._gave_up:
            return
        task = self._tasks.get(key)
        if task is None or task.done():
            self._tasks[key] = self._hass.async_create_background_task(
                self._enforce(key), f"ugreen_powerroam keep {key}"
            )

    def _still_needed(self, key: str) -> bool:
        return key in self._enabled and self._mismatch(key)

    async def _enforce(self, key: str) -> None:
        want = KEEP_TARGETS[key][1]
        await asyncio.sleep(SETTLE_DELAY)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if not self._still_needed(key):
                return
            _LOGGER.warning(
                "%s is %r, wanted %r - sending command (attempt %s/%s)",
                key,
                self._hub.data.get(key),
                want,
                attempt,
                MAX_ATTEMPTS,
            )
            try:
                await self._api.set_device_info(key, want)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - transport errors vary
                _LOGGER.warning("Keep %s: command failed: %s", key, err)
            for _ in range(max(1, round(CONFIRM_TIMEOUT / POLL_STEP))):
                await asyncio.sleep(POLL_STEP)
                if not self._still_needed(key):
                    return
        if self._still_needed(key):
            self._gave_up.add(key)
            _LOGGER.error(
                "Keep %s: still %r after %s attempts, giving up until it changes",
                key,
                self._hub.data.get(key),
                MAX_ATTEMPTS,
            )
