from __future__ import annotations

import logging
from typing import Any, Optional

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

# Try to import DOMAIN from const; fall back to hard-coded if missing.
try:
    from .const import DOMAIN  # type: ignore
except Exception:  # pragma: no cover
    DOMAIN = "windhager"

_LOGGER = logging.getLogger(__name__)


# ----------------------------- Helpers -----------------------------

def _ensure_list(x) -> list:
    return x if isinstance(x, list) else []


def _to_float_or_none(val: Any) -> Optional[float]:
    if val is None:
        return None
    sval = str(val).strip()
    if sval in ("", "-.-", "NaN", "nan", "None"):
        return None
    try:
        return float(sval.replace(",", "."))
    except Exception:
        return None


# --- Minimal mode mapping ------------------------------------------
# Your gateway returns an enum for /3/50/0. Without vendor docs, we keep
# a conservative mapping that worked in field tests. Adjust if needed.
# Example seen in your logs: values "4" and "5" are used.
MODE_TO_HVAC = {
    0: HVACMode.OFF,   # often "off" or standby
    4: HVACMode.AUTO,  # commonly a scheduled/auto program
    5: HVACMode.HEAT,  # comfort/manual heat program
}
HVAC_TO_MODE = {
    HVACMode.OFF: 0,
    HVACMode.AUTO: 4,
    HVACMode.HEAT: 5,
}
# -------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Windhager climate entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    client = data["client"]

    # Use coordinator snapshot if available; else force a fetch.
    snapshot = coordinator.data or {}
    devices = _ensure_list(snapshot.get("devices"))
    if not devices:
        _LOGGER.debug("No devices from coordinator snapshot; forcing initial fetch")
        snapshot = await client.fetch_all()
        devices = _ensure_list(snapshot.get("devices"))

    entities: list[WindhagerClimate] = []

    for dev in devices:
        if dev.get("type") != "climate":
            continue

        try:
            entity = WindhagerClimate(
                coordinator=coordinator,
                client=client,
                device=dev,
            )
            entities.append(entity)
        except Exception as ex:
            _LOGGER.exception("Failed to create climate entity for %s: %s", dev, ex)

    if entities:
        async_add_entities(entities, update_before_add=True)


class WindhagerClimate(CoordinatorEntity, ClimateEntity):
    """HA Climate entity bound to a *single* Windhager function (zone)."""

    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator, client, device: dict[str, Any]) -> None:
        super().__init__(coordinator)
        self._client = client
        self._dev = device

        # Stable identity
        self._attr_unique_id = f"{device['id']}-climate"
        self._attr_name = device.get("name") or "Windhager Climate"

        # Make **each zone a separate HA device** (critical for your setup)
        self._device_id = device.get("device_id") or device["id"]
        self._device_name = device.get("device_name") or self._attr_name
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": self._device_name,
            "manufacturer": "Windhager",
            "model": "InfoWin / RC7030",
        }

        # Build absolute OIDs from the function-specific prefix + relative oids
        prefix = device.get("prefix")  # like "/1/15"
        rel_oids = _ensure_list(device.get("oids"))

        # We expect the relative OIDs in the order your client.py provides:
        # 0: current temp, 1: target temp, 2: mode, 3: custom time, 4: comfort corr
        self._oid_current = f"{prefix}{rel_oids[0]}" if len(rel_oids) > 0 else None
        self._oid_target = f"{prefix}{rel_oids[1]}" if len(rel_oids) > 1 else None
        self._oid_mode   = f"{prefix}{rel_oids[2]}" if len(rel_oids) > 2 else None
        self._oid_custom = f"{prefix}{rel_oids[3]}" if len(rel_oids) > 3 else None
        self._oid_corr   = f"{prefix}{rel_oids[4]}" if len(rel_oids) > 4 else None

        # Internal state mirrors
        self._curr_temp: Optional[float] = None
        self._tgt_temp: Optional[float] = None
        self._mode_val: Optional[int] = None

        # Available HVAC modes we expose (adjust if your plant has others)
        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.AUTO, HVACMode.HEAT]

    # --------------------- State properties ---------------------

    @property
    def hvac_mode(self) -> HVACMode:
        """Return the current HVAC mode mapped from /3/50/0 enum."""
        self._read_snapshot()
        if self._mode_val is None:
            return HVACMode.AUTO  # default if unknown
        return MODE_TO_HVAC.get(self._mode_val, HVACMode.HEAT)

    @property
    def current_temperature(self) -> Optional[float]:
        self._read_snapshot()
        return self._curr_temp

    @property
    def target_temperature(self) -> Optional[float]:
        self._read_snapshot()
        return self._tgt_temp

    # --------------------- Commands (writes) ---------------------

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set hvac mode by writing the function-specific mode OID."""
        if not self._oid_mode:
            _LOGGER.warning("%s has no mode OID; ignoring set_hvac_mode", self.name)
            return
        if hvac_mode not in self.hvac_modes:
            raise ValueError(f"Unsupported HVAC mode: {hvac_mode}")
        mode_val = HVAC_TO_MODE.get(hvac_mode)
        if mode_val is None:
            _LOGGER.warning("No gateway mapping for HVAC mode %s; skipping", hvac_mode)
            return
        await self._client.update(self._oid_mode, mode_val)
        await self.coordinator.async_request_refresh()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set target temperature for this zone only."""
        if not self._oid_target:
            _LOGGER.warning("%s has no target OID; ignoring set_temperature", self.name)
            return
        if ATTR_TEMPERATURE not in kwargs:
            return
        new_t = kwargs[ATTR_TEMPERATURE]
        await self._client.update(self._oid_target, new_t)
        await self.coordinator.async_request_refresh()

    # --------------------- Coordinator → entity sync ---------------------

    def _read_snapshot(self) -> None:
        """Pull the latest values for *this zone only* from coordinator.data['oids']."""
        data = self.coordinator.data or {}
        oids = data.get("oids") or {}
        if self._oid_current:
            self._curr_temp = _to_float_or_none(oids.get(self._oid_current))
        if self._oid_target:
            self._tgt_temp = _to_float_or_none(oids.get(self._oid_target))
        if self._oid_mode:
            try:
                raw = oids.get(self._oid_mode)
                self._mode_val = int(str(raw)) if raw is not None and str(raw).strip() != "" else None
            except Exception:
                self._mode_val = None

    async def async_update(self) -> None:
        """Let CoordinatorEntity drive updates; no direct I/O here."""
        # Coordinator handles fetching; we just mirror values.
        return
