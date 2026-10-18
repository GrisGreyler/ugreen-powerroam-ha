"""Sensors for the UGREEN PowerRoam."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BLE_SENSORS, DOMAIN, SENSORS, SUGGESTED_HOURS, TRANSPORT_BLE


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    api, hub = data["api"], data["hub"]

    # Only offer sensors this transport can actually populate, so neither setup
    # is left with permanently unavailable entities.
    keys = SENSORS if data["transport"] != TRANSPORT_BLE else BLE_SENSORS
    async_add_entities(UgreenSensor(api, hub, key) for key in SENSORS if key in keys)
    async_add_entities([UgreenCellImbalanceSensor(api, hub)])


class UgreenSensor(SensorEntity):
    _attr_should_poll = False

    def __init__(self, api, hub, key: str) -> None:
        name, unit, device_class, state_class, entity_category, icon = SENSORS[key]
        self._hub = hub
        self._key = key
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_entity_category = (
            EntityCategory(entity_category) if entity_category else None
        )
        self._attr_icon = icon
        unique_key = key
        if key.startswith("cell_voltage_"):
            self._attr_suggested_display_precision = 3
        if key in SUGGESTED_HOURS:
            self._attr_suggested_unit_of_measurement = "h"
            self._attr_suggested_display_precision = 1
            # Bumped so this doesn't reattach to the orphaned registry entry
            # from before the unit was seconds - that entry has "s" baked
            # into its cached suggested_unit_of_measurement.
            unique_key = f"{key}_v2"
        self._attr_unique_id = f"{api.unique_id_base}_{unique_key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, api.unique_id_base)},
            name="UGREEN PowerRoam",
            manufacturer="UGREEN",
            model=api.device_name,
            serial_number=api.sn,
        )

    @property
    def native_value(self):
        data = self._hub.data
        if self._key == "discharge_remain_time" and not data.get("discharge_pow"):
            return None
        if self._key == "charge_remain_time" and not data.get("charge_power_all"):
            return None
        return data.get(self._key)

    @property
    def available(self) -> bool:
        return self._hub.available and self._key in self._hub.data

    async def async_added_to_hass(self) -> None:
        self._remove_listener = self._hub.add_listener(self.async_write_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        self._remove_listener()


class UgreenCellImbalanceSensor(UgreenSensor):
    """Derived: spread between the highest and lowest cell voltage (mV)."""

    _CELL_KEYS = tuple(f"cell_voltage_{n}" for n in range(1, 8))

    def __init__(self, api, hub) -> None:
        super().__init__(api, hub, "cell_voltage_1")
        self._key = "cell_imbalance"
        self._attr_name = "Cell Imbalance"
        self._attr_device_class = None
        self._attr_native_unit_of_measurement = "mV"
        self._attr_suggested_display_precision = 0
        self._attr_icon = "mdi:scale-unbalanced"
        self._attr_unique_id = f"{api.unique_id_base}_cell_imbalance"

    def _cells(self) -> list[float]:
        return [
            v
            for k in self._CELL_KEYS
            if isinstance(v := self._hub.data.get(k), (int, float))
        ]

    @property
    def native_value(self):
        cells = self._cells()
        if len(cells) < 2:
            return None
        spread = max(cells) - min(cells)
        # values in volts (e.g. 3.341) -> convert to mV
        if max(cells) < 10:
            spread *= 1000
        return round(spread)

    @property
    def available(self) -> bool:
        return self._hub.available and len(self._cells()) > 1
