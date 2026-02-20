from __future__ import annotations

from typing import Any, Optional
import logging

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import (
    ATTR_TEMPERATURE,
    UnitOfTemperature,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback

_LOGGER = logging.getLogger(__name__)

# Try to import DOMAIN from const; fall back if missing.
try:
    from .const import DOMAIN  # type: ignore
except Exception:  # pragma: no cover
    DOMAIN = "windhager"


# ----------------------------- helpers -----------------------------

def _ensure_list(x) -> list:
    return x if isinstance(x, list) else []


def _to_float_or_none(val: Any) -> Optional[float]:
    if val is None:
        return None
    s = str(val).strip()
    if s in ("", "-.-", "NaN", "nan", "None"):
        return None
    try:
        return float(s.replace(",", "."))
    except Exception:
        return None


# Minimal mode mapping (adjust if your gateway uses different enums)
# Your logs showed values like 4 and 5. We map:
# 0 → OFF, 4 → AUTO (scheduled), 5 → HEAT (comfort/manual)
MODE_TO_HVAC = {
    0: HVACMode.OFF,
    4: HVACMode.AUTO,
    5: HVACMode.HEAT,
}
HVAC_TO_MODE = {
    HVACMode.OFF: 0,
    HVACMode.AUTO: 4,
    HVACMode.HEAT: 5,
}


# --------------------------- setup entry ---------------------------

async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    client = data["client"]

    snapshot = coordinator.data or {}
    devices = _ensure_list(snapshot.get("devices"))
    if not devices:
        _LOGGER.debug("Windhager climate: no devices in coordinator snapshot; pulling once")
        snapshot = await client.fetch_all()
        devices = _ensure_list(snapshot.get("devices"))

    entities: list[WindhagerClimate] = []
    for dev in devices:
        if dev.get("type") != "climate":
            continue
        try:
            entities.append(WindhagerClimate(coordinator, client, dev))
        except Exception as ex:
            _LOGGER.exception("Windhager climate: failed to create entity for %s: %s", dev, ex)

    if entities:
        async_add_entities(entities, update_before_add=True)


# ---------------------------- entity -----------------------------

class WindhagerClimate(CoordinatorEntity, ClimateEntity):
    """One HA Climate entity bound to a single Windhager function (zone)."""

    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator, client, device: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self._client = client
        self._dev = device

        # Entity identity
        self._attr_unique_id = f"{device['id']}-climate"
        self._attr_name = device.get("name") or "Windhager Climate"

        # Make each zone a separate HA device (prevents OG1 changing Parterre/WW)
        device_id = device.get("device_id") or device["id"]
        device_name = device.get("device_name") or self._attr_name
        self._attr_device_info = {
            "identifiers": {(DOMAIN, device_id)},
            "name": device_name,
            "manufacturer": "Windhager",
            "model": "InfoWin / RC7030",
        }

        # Function-specific OIDs: prefix + relative oids from client.py
        prefix = device.get("prefix")  # e.g. "/1/15"
        rel = _ensure_list(device.get("oids"))

        # 0: current temp, 1: target temp, 2: mode, 3: custom time, 4: comfort corr
        self._oid_current = f"{prefix}{rel[0]}" if len(rel) > 0 else None
        self._oid_target  = f"{prefix}{rel[1]}" if len(rel) > 1 else None
        self._oid_mode    = f"{prefix}{rel[2]}" if len(rel) > 2 else None
        self._oid_custom  = f"{prefix}{rel[3]}" if len(rel) > 3 else None
        self._oid_corr    = f"{prefix}{rel[4]}" if len(rel) > 4 else None

        # Mirrors
        self._curr_temp: Optional[float] = None
        self._tgt_temp: Optional[float] = None
        self._mode_val: Optional[int] = None

        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.AUTO, HVACMode.HEAT]

    # --------------------- state properties ---------------------

    @property
    def hvac_mode(self) -> HVACMode:
        self._pull_snapshot()
        if self._mode_val is None:
            return HVACMode.AUTO
        return MODE_TO_HVAC.get(self._mode_val, HVACMode.HEAT)

    @property
    def current_temperature(self) -> Optional[float]:
        self._pull_snapshot()
        return self._curr_temp

    @property
    def target_temperature(self) -> Optional[float]:
        self._pull_snapshot()
        return self._tgt_temp

    # ----------------------- write commands ----------------------

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if not self._oid_mode:
            _LOGGER.warning("%s has no mode OID; ignoring set_hvac_mode", self.name)
            return
        if hvac_mode not in self.hvac_modes:
            raise ValueError(f"Unsupported HVAC mode: {hvac_mode}")
        val = HVAC_TO_MODE.get(hvac_mode)
        if val is None:
            _LOGGER.warning("No mapping for HVAC mode %s; skipping", hvac_mode)
            return
        await self._client.update(self._oid_mode, val)
        await self.coordinator.async_request_refresh()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if not self._oid_target:
            _LOGGER.warning("%s has no target OID; ignoring set_temperature", self.name)
            return
        if ATTR_TEMPERATURE not in kwargs:
            return
        new_t = kwargs[ATTR_TEMPERATURE]
        await self._client.update(self._oid_target, new_t)
        await self.coordinator.async_request_refresh()

    # --------------------- coordinator snapshot ------------------

    def _pull_snapshot(self) -> None:
        data = self.coordinator.data or {}
        oids = data.get("oids") or {}
        if self._oid_current:
            self._curr_temp = _to_float_or_none(oids.get(self._oid_current))
        if self._oid_target:
            self._tgt_temp = _to_float_or_none(oids.get(self._oid_target))
        if self._oid_mode:
            raw = oids.get(self._oid_mode)
            try:
                self._mode_val = int(str(raw)) if raw not in (None, "") else None
            except Exception:
                self._mode_val = None

    async def async_update(self) -> None:
        # CoordinatorEntity pulls data; no direct I/O here.
        return
