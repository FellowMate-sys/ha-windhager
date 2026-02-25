import aiohttp
import json
import logging
from pathlib import Path
from .aiohelper import DigestAuth
from .const import DEFAULT_USERNAME, DOMAIN

_LOGGER = logging.getLogger(__name__)


class WindhagerHttpClient:
    """HTTP client for the RC7030 API, fully driven by spec.json."""

    def __init__(self, host, password) -> None:
        self.host = host
        self.password = password

        self._session = None
        self._auth = None

        # Loaded on init from spec.json
        self._spec = self._load_spec()
        self._unknown_values = set(self._spec.get("unknown_values", ["-.-", ""]))
        # Runtime-default Eco/Comfort duration (editable via service)
        self._eco_default_duration_minutes = int(
            self._spec.get("eco_default_duration_minutes", 180)
        )

        # These are rebuilt on each fetch_all
        self._oids_to_fetch = set()
        self.devices = []

    # ---------------------- Public properties ----------------------

    @property
    def eco_default_duration_minutes(self) -> int:
        """Default Eco/Comfort duration used by climate set_temperature."""
        return self._eco_default_duration_minutes

    def set_eco_default_duration_minutes(self, minutes: int) -> None:
        """Update the runtime default Eco/Comfort duration (service can call this)."""
        try:
            minutes = int(minutes)
            if minutes <= 0:
                raise ValueError
            self._eco_default_duration_minutes = minutes
            _LOGGER.info("Eco/Comfort default duration set to %s minutes", minutes)
        except Exception:
            _LOGGER.error("Invalid Eco/Comfort default duration: %s", minutes)

    # ---------------------- Internal helpers ----------------------

    def _load_spec(self) -> dict:
        """Load spec.json located next to this file."""
        try:
            path = Path(__file__).with_name("spec.json")
            with path.open("r", encoding="utf-8") as f:
                spec = json.load(f)
            _LOGGER.debug("Loaded spec.json from %s", path)
            return spec
        except Exception as e:
            _LOGGER.error("Failed to load spec.json: %s", e)
            # Fallback to empty spec so HA stays alive
            return {"heating_circuits": [], "modules": []}

    async def _ensure_session(self):
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._auth = DigestAuth(DEFAULT_USERNAME, self.password, self._session)

    async def close(self):
        if self._session:
            await self._session.close()
            self._session = None
            self._auth = None

    async def fetch(self, oid: str):
        """GET /api/1.0/lookup<oid>"""
        await self._ensure_session()
        url = f"http://{self.host}/api/1.0/lookup{oid}"
        try:
            ret = await self._auth.request("GET", url)
            js = await ret.json()
            _LOGGER.debug("Lookup %s -> %s", oid, js)
            return js
        except Exception as e:
            _LOGGER.error("Lookup failed for %s: %s", oid, e)
            return None

    async def update(self, oid: str, value):
        """PUT /api/1.0/datapoint"""
        await self._ensure_session()
        payload = bytes(f'{{"OID":"{oid}","value":"{value}"}}', "utf-8")
        try:
            await self._auth.request("PUT", f"http://{self.host}/api/1.0/datapoint", data=payload)
            _LOGGER.debug("PUT %s = %s", oid, value)
        except Exception as e:
            _LOGGER.error("Failed to update %s: %s", oid, e)
            raise

    @staticmethod
    def slugify(identifier_str: str) -> str:
        return identifier_str.replace(".", "-").replace("/", "-")

    # ---------------------- Device builders ----------------------

    def _build_hk_climate_device(self, hk: dict):
        """Create a climate device (and child sensors) from a HK spec block."""
        name = hk["name"]
        node = hk["node"]
        fct = hk["fct"]
        oids = hk["oids"]

        prefix = f"/1/{node}/{fct}"
        dev_id = self.slugify(f"{self.host}{prefix}")

        # Parent climate device
        self.devices.append({
            "id": dev_id,
            "name": name,
            "type": "climate",
            "prefix": prefix,  # kept for backward compatibility
            # Explicit, semantic OIDs used by climate.py
            "oids_map": {
                "mode": oids.get("mode"),
                "comfort_offset": oids.get("comfort_offset"),
                "eco_temp": oids.get("eco_temp"),
                "eco_duration": oids.get("eco_duration"),
                "room_temp": oids.get("room_temp"),
                "room_target_ro": oids.get("room_target_ro")
            },
            "device_id": dev_id,
            "device_name": name,
        })

        # Register control/read OIDs
        for k in ("mode", "comfort_offset", "eco_temp", "eco_duration", "room_temp", "room_target_ro"):
            if oids.get(k):
                self._oids_to_fetch.add(oids[k])

        # Temperature child sensors
        for key, nice in [
            ("room_temp", f"{name} Room Temperature"),
            ("room_target_ro", f"{name} Target Temperature (read-only)"),
            ("flow_temp", f"{name} Flow Temperature"),
            ("flow_target", f"{name} Flow Target"),
            ("dhw_temp", f"{name} DHW Temperature"),
            ("dhw_target_ro", f"{name} DHW Target (read-only)"),
            ("outside_temp", f"{name} Outside Temperature"),
        ]:
            oid = oids.get(key)
            if oid:
                self.devices.append({
                    "id": self.slugify(f"{self.host}{oid}"),
                    "name": nice,
                    "type": "temperature",
                    "oid": oid,
                    "device_id": dev_id,
                    "device_name": name,
                })
                self._oids_to_fetch.add(oid)

        # Status sensors (non-temperature)
        for key, nice in [
            ("pump", f"{name} Pump"),
            ("mixer", f"{name} Mixer"),
        ]:
            oid = oids.get(key)
            if oid:
                self.devices.append({
                    "id": self.slugify(f"{self.host}{oid}"),
                    "name": nice,
                    "type": "sensor",
                    "device_class": None,
                    "state_class": None,
                    "unit": None,
                    "oid": oid,
                    "device_id": dev_id,
                    "device_name": name,
                })
                self._oids_to_fetch.add(oid)

    def _build_module_sensors(self, module: dict):
        """Create a non-climate device (AeroWIN / LogWIN / Hybrid) with read-only sensors."""
        name = module["name"]
        node = module["node"]
        fct  = module["fct"]
        prefix = f"/1/{node}/{fct}"
        dev_id = self.slugify(f"{self.host}{prefix}")

        for s in module.get("sensors", []):
            oid = s["oid"]
            s_name = s["name"]
            # Heuristic: label as temperature if name contains "temperatur"
            typ = "temperature" if "temperatur" in s_name.lower() else "sensor"
            self.devices.append({
                "id": self.slugify(f"{self.host}{oid}"),
                "name": f"{name} {s_name}",
                "type": typ,
                "oid": oid,
                "device_id": dev_id,
                "device_name": name,
            })
            self._oids_to_fetch.add(oid)

    # ---------------------- Main entry for coordinator ----------------------

    async def fetch_all(self):
        """Build device list from spec.json and fetch all referenced OIDs once."""
        # Reset per-cycle
        self.devices = []
        self._oids_to_fetch = set()

        # Build from spec (no discovery)
        for hk in self._spec.get("heating_circuits", []):
            self._build_hk_climate_device(hk)
        for mod in self._spec.get("modules", []):
            self._build_module_sensors(mod)

        # Now look up all OIDs in one pass
        ret = {"devices": self.devices, "oids": {}, "units": {}, "meta": {
            "eco_default_duration_minutes": self._eco_default_duration_minutes
        }}

        for oid in sorted(self._oids_to_fetch):
            js = await self.fetch(oid)
            if not js or "value" not in js:
                ret["oids"][oid] = None
                continue
            val = js.get("value")
            unit = js.get("unit")
            ret["units"][oid] = unit
            if val in self._unknown_values:
                _LOGGER.debug("Invalid or missing value for OID %s: %s", oid, js)
                ret["oids"][oid] = None
            else:
                ret["oids"][oid] = val

        return ret