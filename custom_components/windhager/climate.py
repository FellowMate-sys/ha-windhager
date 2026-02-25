"""
Windhager Climate Entity (Data-Driven Edition)
Compatible with spec.json + new client.py
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import voluptuous as vol

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
    HVACAction,
)
from homeassistant.components.climate.const import ATTR_TEMPERATURE
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback, entity_platform
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN
from .helpers import get_oid_value

_LOGGER = logging.getLogger(__name__)

DEFAULT_TEMP_STEP = 0.5
FALLBACK_ECO_MINUTES = 180


# ---------------------- Helper functions ---------------------- #

def _float_or_none(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def map_mode_from_raw(raw: Any) -> HVACMode:
    """
    Betriebsart raw → HA HVACMode
    Adjust mapping here once you confirm your real values.
    """
    if str(raw) == "0":
        return HVACMode.AUTO
    if str(raw) == "2":
        return HVACMode.OFF
    return HVACMode.HEAT  # default


def map_mode_to_raw(mode: HVACMode) -> str:
    """HA HVACMode → Betriebsart raw"""
    if mode == HVACMode.AUTO:
        return "0"
    if mode == HVACMode.OFF:
        return "2"
    return "1"  # HEAT


def _get_runtime_eco_minutes(coordinator) -> int:
    """
    Get default Eco/Comfort duration from:
    1) httpClient.eco_default_duration_minutes
    2) coordinator.data["meta"]
    3) fallback
    """
    http = getattr(coordinator, "httpClient", None)
    if http and hasattr(http, "eco_default_duration_minutes"):
        try:
            val = int(http.eco_default_duration_minutes)
            if val > 0:
                return val
        except Exception:
            pass

    data = getattr(coordinator, "data", {}) or {}
    meta = data.get("meta") or {}
    try:
        val = int(meta.get("eco_default_duration_minutes", FALLBACK_ECO_MINUTES))
        if val > 0:
            return val
    except Exception:
        pass

    return FALLBACK_ECO_MINUTES


# ==============================================================
# SETUP
# ==============================================================

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback
) -> None:

    platform = entity_platform.async_get_current_platform()

    # Register bias override service
    platform.async_register_entity_service(
        "set_current_temp_compensation",
        {
            vol.Required("compensation"): vol.All(
                vol.Coerce(float), vol.Range(min=-3.5, max=3.5)
            )
        },
        "set_current_temp_compensation",
    )

    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[ClimateEntity] = []

    for device in coordinator.data.get("devices", []):
        if device.get("type") == "climate":
            # standard entity
            entities.append(WindhagerClimate(coordinator, device))
            # no-bias version
            entities.append(WindhagerClimateNoBias(coordinator, device))

    if entities:
        async_add_entities(entities)



# ==============================================================
# BASE ENTITY
# ==============================================================

class WindhagerClimateBase(CoordinatorEntity, ClimateEntity):
    """
    Base class for data-driven Windhager climate entities.
    """

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = DEFAULT_TEMP_STEP
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE |
        ClimateEntityFeature.TURN_ON |
        ClimateEntityFeature.TURN_OFF
    )
    _attr_hvac_modes = [HVACMode.AUTO, HVACMode.HEAT, HVACMode.OFF]

    def __init__(self, coordinator, device: dict):
        super().__init__(coordinator)

        self._device = device
        self._oids = device.get("oids_map") or {}
        self._prefix = device.get("prefix", "")
        self._http = coordinator.httpClient

        dev_id = device.get("device_id") or device.get("id")
        name = device.get("device_name") or device.get("name") or "Windhager Climate"

        self._attr_unique_id = device.get("id")
        self._attr_name = name

        self._device_info = DeviceInfo(
            identifiers={(DOMAIN, dev_id)},
            name=name,
            manufacturer="Windhager",
            model="MES Infinity (RC7030)",
        )

    # ------------------ Availability ------------------ #
    @property
    def available(self) -> bool:
        o = (self.coordinator.data or {}).get("oids", {})
        return o.get(self._oids.get("room_temp")) is not None

    # ------------------ Temperature getters ------------------ #
    @property
    def current_temperature(self) -> float | None:
        oid = self._oids.get("room_temp")
        return _float_or_none((self.coordinator.data or {}).get("oids", {}).get(oid))

    @property
    def target_temperature(self) -> float | None:
    
