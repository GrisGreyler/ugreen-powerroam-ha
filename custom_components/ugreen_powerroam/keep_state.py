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
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# telemetry key -> (switch name, wanted raw value)
# The device page lists entities alphabetically by name, so each name starts
# with the name of the control it holds - that puts it right below it.
KEEP_TARGETS: dict[str, tuple[str, int]] = {
    "switch_ac": ("AC Output Keep On", 1),
    "usb_sw": ("USB Output Keep On", 1),
    "work_mode": ("Always On Mode Keep On", 3),
}

SETTLE_DELAY = 5  # let a rebooted unit finish publishing its state first
POLL_STEP = 5
CONFIRM_TIMEOUT = 45  # telemetry ~15 s plus the hub's 15 s change debounce
MAX_ATTEMPTS = 5


class KeepShared:
    """State shared by every KeepState of one station (cloud + Bluetooth)."""

    def __init__(self) -> None:
        self.enabled: set[str] = set()
        self.listeners: list[Callable[[], None]] = []
        self.instances: list[KeepState] = []
        self.enforcing: set[str] = set()
        self.last_done: dict[str, tuple[object, float]] = {}


class KeepState:
    def __init__(
        self, hass: HomeAssistant, hub, api, shared: KeepShared | None = None
    ) -> None:
        self._hass = hass
        self._hub = hub
        self._api = api
        self._shared = shared or KeepShared()
        self._enabled = self._shared.enabled
        self._listeners = self._shared.listeners
        self._tasks: dict[str, asyncio.Task] = {}
        self._gave_up: set[str] = set()
        self._unsub: Callable[[], None] | None = None

    def start(self) -> None:
        self._shared.instances.append(self)
        self._unsub = self._hub.add_listener(self._on_update)

    async def stop(self) -> None:
        if self in self._shared.instances:
            self._shared.instances.remove(self)
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
        if value:
            self._enabled.add(key)
        else:
            self._enabled.discard(key)
        instances = list(self._shared.instances) or [self]
        for inst in instances:
            inst._gave_up.discard(key)
            if not value:
                inst._cancel(key)
        for cb in list(self._listeners):
            cb()
        if value:
            for inst in instances:
                inst._check(key)

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
                self._enforce_once(key), f"ugreen_powerroam keep {key}"
            )

    def _still_needed(self, key: str) -> bool:
        return key in self._enabled and self._mismatch(key)

    async def _enforce_once(self, key: str) -> None:
        """Let only one entry at a time fix a key on the shared station."""
        shared = self._shared
        owner, when = shared.last_done.get(key, (None, 0.0))
        if key in shared.enforcing or (
            owner is not None
            and owner is not self
            and time.monotonic() - when < CONFIRM_TIMEOUT
        ):
            return
        shared.enforcing.add(key)
        try:
            await self._enforce(key)
        finally:
            shared.enforcing.discard(key)
            shared.last_done[key] = (self, time.monotonic())

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
            except Exception as err:
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
