import aiohttp
import logging
from .aiohelper import DigestAuth
from .const import DEFAULT_USERNAME, CLIMATE_FUNCTION_TYPE, HEATER_FUNCTION_TYPE

_LOGGER = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Optional: enable a one-time scan that logs every °C OID found under AeroWIN/LogWIN.
# Leave False for normal operation. Set to True once to discover Storage/DHW OIDs.
SCAN_TEMP_CANDIDATES = False
TEMP_SCAN_NODES = (60, 65)   # Commonly AeroWIN (60) and LogWIN (65); adjust if needed
TEMP_SCAN_GROUPS = (0, 1)    # Groups where temp datapoints often live
TEMP_SCAN_MEMBERS = range(0, 120)
# -----------------------------------------------------------------------------


# -------------------------- Value parsing utilities ---------------------------
def _is_missing_or_sentinel(val: object) -> bool:
    """Return True if a value is missing or a known sentinel like '-.-' or blank."""
    if val is None:
        return True
    sval = str(val).strip()
    return sval in ("", "-.-", "NaN", "nan", "None")


# ----------------------- Discovery helpers for /1 payload ---------------------
def iter_um_zones_from_devices(devices):
    """
    Yield dicts for ALL unlocked climate functions (fctType == CLIMATE_FUNCTION_TYPE)
    across all nodes discovered in /1.
    Each item: {"node": <nodeId>, "fct_id": <fctId>, "label": <friendly name>}
    """
    if not isinstance(devices, list):
        return
    for dev in devices:
        try:
            node_id = dev.get("nodeId")
            functions = dev.get("functions", [])
            for f in functions:
                if f.get("fctType") == CLIMATE_FUNCTION_TYPE and not f.get("lock", False):
                    yield {
                        "node": node_id,
                        "fct_id": f.get("fctId"),
                        "label": f.get("name") or f"Zone {node_id}",
                    }
        except Exception:
            # Continue on any malformed device entry
            continue


async def scan_temp_candidates_for_nodes(client, nodes=TEMP_SCAN_NODES,
                                         groups=TEMP_SCAN_GROUPS,
                                         members=TEMP_SCAN_MEMBERS):
    """
    Log every OID under given nodes that returns a valid °C value.
    Use this once to identify Storage and DHW tank temperature OIDs.
    """
    for node in nodes:
        for grp in groups:
            for mem in members:
                oid = f"/1/{node}/0/{grp}/{mem}/0"
                try:
                    data = await client.fetch(oid)
                except Exception:
                    continue
                if not data:
                    continue
                unit = data.get("unit")
                val = data.get("value")
                if unit == "°C" and not _is_missing_or_sentinel(val):
                    _LOGGER.debug(
                        "TEMP-CANDIDATE node=%s grp=%s mem=%s OID=%s -> %s°C name=%s",
                        node, grp, mem, oid, val, data.get("name")
                    )


class WindhagerHttpClient:
    """Raw API HTTP requests and discovery/aggregation for the Windhager gateway."""

    def __init__(self, host, password) -> None:
        self.host = host
        self.password = password
        self.oids = None               # set of OIDs to read
        self.devices = []              # list of entity descriptors for HA
        self._session = None
        self._auth = None
        self._did_temp_scan = False    # run the temp scan only once per process

    async def _ensure_session(self):
        """Ensure that we have an active client session."""
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._auth = DigestAuth(DEFAULT_USERNAME, self.password, self._session)

    async def close(self):
        """Close the client session."""
        if self._session:
            await self._session.close()
            self._session = None
            self._auth = None

    async def fetch(self, url):
        """
        Fetch a lookup endpoint.
        Example: url="/1" or url="/1/15/0/1/1/0"
        """
        try:
            await self._ensure_session()
            ret = await self._auth.request(
                "GET", f"http://{self.host}/api/1.0/lookup{url}"
            )
            json = await ret.json()
            _LOGGER.debug("Fetched data for %s: %s", url, json)
            return json
        except Exception as e:
            _LOGGER.error("Failed to fetch data for %s: %s", url, str(e))
            raise

    async def update(self, oid, value):
        """Write a value back to a datapoint OID."""
        await self._ensure_session()
        await self._auth.request(
            "PUT",
            f"http://{self.host}/api/1.0/datapoint",
            data=bytes(f'{{"OID":"{oid}","value":"{value}"}}', "utf-8"),
        )

    @staticmethod
    def slugify(identifier_str):
        return identifier_str.replace(".", "-").replace("/", "-")

    async def fetch_all(self):
        """
        High-level:
        1) On first call, discover all devices via /1 and build an entity list:
           - ALL unlocked climate functions => one climate entity per function
           - ALL unlocked heater functions => sensors for each heater
           Collect all required OIDs into self.oids (a set).
        2) Read every OID once and return:
           { "devices": <entity descriptors>, "oids": { <oid>: value_or_None } }
        """
        # ---------------------- First-time discovery --------------------------
        if self.oids is None:
            self.oids = set()
            self.devices = []

            # Discover gateway devices/functions
            json_devices = await self.fetch("/1")
            if not isinstance(json_devices, list):
                _LOGGER.warning("Unexpected /1 discovery payload: %s", type(json_devices))
                json_devices = []

            # --------- CLIMATE: build entities for ALL unlocked climate funcs
            for dev in json_devices:
                node_id = dev.get("nodeId")
                device_id_prefix = f"/1/{str(node_id)}"
                functions = dev.get("functions", [])

                climate_functions = [
                    f for f in functions
                    if f.get("fctType") == CLIMATE_FUNCTION_TYPE and not f.get("lock", False)
                ]

                for fct in climate_functions:
                    fct_id_str = f"/{str(fct.get('fctId'))}"
                    zone_name = fct.get("name") or "Climate zone"

                    # "climate" controller descriptor (bundle of OIDs)
                    self.devices.append(
                        {
                            "id": self.slugify(f"{self.host}{device_id_prefix}{fct_id_str}"),
                            "name": zone_name,
                            "type": "climate",
                            "prefix": device_id_prefix,
                            "oids": [
                                f"{fct_id_str}/0/1/0",  # current room temp
                                f"{fct_id_str}/1/1/0",  # target temp
                                f"{fct_id_str}/3/50/0", # operating mode enum
                                f"{fct_id_str}/2/10/0", # custom temp duration (min)
                                f"{fct_id_str}/3/58/0", # comfort correction
                            ],
                            "device_id": self.slugify(f"{self.host}{device_id_prefix}"),
                            "device_name": zone_name,
                        }
                    )

                    # OIDs required by this climate
                    self.oids.update(
                        [
                            f"{device_id_prefix}{fct_id_str}/0/1/0",  # current temp
                            f"{device_id_prefix}{fct_id_str}/1/1/0",  # target temp
                            f"{device_id_prefix}{fct_id_str}/3/50/0", # mode
                            f"{device_id_prefix}{fct_id_str}/2/10/0", # custom duration
                            f"{device_id_prefix}{fct_id_str}/0/0/0",  # outside temp (replicated)
                            f"{device_id_prefix}{fct_id_str}/3/58/0", # comfort correction
                            f"{device_id_prefix}{fct_id_str}/3/7/0",  # temperature correction
                        ]
                    )

                    # Current temperature (with correction reference)
                    self.devices.append(
                        {
                            "id": self.slugify(
                                f"{self.host}{device_id_prefix}{fct_id_str}/0/1/0/3/58/0"
                            ),
                            "name": f"{zone_name} Current Temperature",
                            "type": "temperature",
                            "correction_oid": f"{device_id_prefix}{fct_id_str}/3/58/0",
                            "oid": f"{device_id_prefix}{fct_id_str}/0/1/0",
                            "device_id": self.slugify(f"{self.host}{node_id}"),
                            "device_name": zone_name,
                        }
                    )

                    # Current temperature (raw/real)
                    self.devices.append(
                        {
                            "id": self.slugify(
                                f"{self.host}{device_id_prefix}{fct_id_str}/0/1/0"
                            ),
                            "name": f"{zone_name} Current Temperature real",
                            "type": "temperature",
                            "oid": f"{device_id_prefix}{fct_id_str}/0/1/0",
                            "device_id": self.slugify(f"{self.host}{node_id}"),
                            "device_name": zone_name,
                        }
                    )

                    # Comfort temperature correction
                    self.devices.append(
                        {
                            "id": self.slugify(
                                f"{self.host}{device_id_prefix}{fct_id_str}/3/58/0"
                            ),
                            "name": f"{zone_name} Comfort Temperature Correction",
                            "type": "sensor",
                            "device_class": None,
                            "state_class": None,
                            "unit": "K",
                            "oid": f"{device_id_prefix}{fct_id_str}/3/58/0",
                            "device_id": self.slugify(f"{self.host}{node_id}"),
                            "device_name": zone_name,
                        }
                    )

                    # Current temperature correction
                    self.devices.append(
                        {
                            "id": self.slugify(
                                f"{self.host}{device_id_prefix}{fct_id_str}/3/7/0"
                            ),
                            "name": f"{zone_name} Current Temperature Correction",
                            "type": "sensor",
                            "device_class": None,
                            "state_class": None,
                            "unit": "K",
                            "oid": f"{device_id_prefix}{fct_id_str}/3/7/0",
                            "device_id": self.slugify(f"{self.host}{node_id}"),
                            "device_name": zone_name,
                        }
                    )

                    # Target temperature
                    self.devices.append(
                        {
                            "id": self.slugify(
                                f"{self.host}{device_id_prefix}{fct_id_str}/1/1/0"
                            ),
                            "name": f"{zone_name} Target Temperature",
                            "type": "temperature",
                            "correction_oid": f"{device_id_prefix}{fct_id_str}/3/58/0",
                            "oid": f"{device_id_prefix}{fct_id_str}/1/1/0",
                            "device_id": self.slugify(f"{self.host}{node_id}"),
                            "device_name": zone_name,
                        }
                    )

                    # Outside temperature
                    self.devices.append(
                        {
                            "id": self.slugify(
                                f"{self.host}{device_id_prefix}{fct_id_str}/0/0/0"
                            ),
                            "name": f"{zone_name} Outside Temperature",
                            "type": "temperature",
                            "oid": f"{device_id_prefix}{fct_id_str}/0/0/0",
                            "device_id": self.slugify(f"{self.host}{node_id}"),
                            "device_name": zone_name,
                        }
                    )

            # --------- HEATERS: build sensors for ALL unlocked heater funcs
            for dev in json_devices:
                node_id = dev.get("nodeId")
                device_id_prefix = f"/1/{str(node_id)}"
                functions = dev.get("functions", [])

                heater_functions = [
                    f for f in functions
                    if f.get("fctType") == HEATER_FUNCTION_TYPE and not f.get("lock", False)
                ]

                for fct in heater_functions:
                    fct_id_str = f"/{str(fct.get('fctId'))}"
                    heater_name = fct.get("name") or "Heater"

                    # Collect heater OIDs
                    self.oids.update(
                        [
                            f"{device_id_prefix}{fct_id_str}/0/9/0",   # power (%)
                            f"{device_id_prefix}{fct_id_str}/0/11/0",  # fumes temp
                            f"{device_id_prefix}{fct_id_str}/0/7/0",   # heater temp
                            f"{device_id_prefix}{fct_id_str}/0/45/0",  # combustion chamber temp
                            f"{device_id_prefix}{fct_id_str}/2/1/0",   # heater status
                            f"{device_id_prefix}{fct_id_str}/23/100/0",# pellet cons.
                            f"{device_id_prefix}{fct_id_str}/23/103/0",# total pellet cons.
                            f"{device_id_prefix}{fct_id_str}/20/61/0", # time to cleaning 1
                            f"{device_id_prefix}{fct_id_str}/20/62/0", # time to cleaning 2
                        ]
                    )

                    # Heater current power factor
                    self.devices.append(
                        {
                            "id": self.slugify(f"{self.host}{device_id_prefix}{fct_id_str}/0/9/0"),
                            "name": f"{heater_name} Power factor",
                            "type": "sensor",
                            "device_class": "power_factor",
                            "state_class": None,
                            "unit": "%",
                            "oid": f"{device_id_prefix}{fct_id_str}/0/9/0",
                            "device_id": self.slugify(f"{self.host}{node_id}"),
                            "device_name": heater_name,
                        }
                    )
                    # Fumes temperature
                    self.devices.append(
                        {
                            "id": self.slugify(f"{self.host}{device_id_prefix}{fct_id_str}/0/11/0"),
                            "name": f"{heater_name} Fumes Temperature",
                            "type": "temperature",
                            "oid": f"{device_id_prefix}{fct_id_str}/0/11/0",
                            "device_id": self.slugify(f"{self.host}{node_id}"),
                            "device_name": heater_name,
                        }
                    )
                    # Heater temperature
                    self.devices.append(
                        {
                            "id": self.slugify(f"{self.host}{device_id_prefix}{fct_id_str}/0/7/0"),
                            "name": f"{heater_name} Heater Temperature",
                            "type": "temperature",
                            "oid": f"{device_id_prefix}{fct_id_str}/0/7/0",
                            "device_id": self.slugify(f"{self.host}{node_id}"),
                            "device_name": heater_name,
                        }
                    )
                    # Combustion chamber temperature
                    self.devices.append(
                        {
                            "id": self.slugify(f"{self.host}{device_id_prefix}{fct_id_str}/0/45/0"),
                            "name": f"{heater_name} Combustion chamber Temperature",
                            "type": "temperature",
                            "oid": f"{device_id_prefix}{fct_id_str}/0/45/0",
                            "device_id": self.slugify(f"{self.host}{node_id}"),
                            "device_name": heater_name,
                        }
                    )
                    # Heater status
                    self.devices.append(
                        {
                            "id": self.slugify(f"{self.host}{device_id_prefix}{fct_id_str}/2/1/0"),
                            "name": f"{heater_name} Heater status",
                            "options": [
                                "Brûleur bloqué",
                                "Autotest",
                                "Eteindre gén. chaleur",
                                "Veille",
                                "Brûleur ARRET",
                                "Prérinçage",
                                "Phase d'allumage",
                                "Stabilisation flamme",
                                "Mode modulant",
                                "Chaudière bloqué",
                                "Veille temps différé",
                                "Ventilateur Arrêté",
                                "Porte de revêtement ouverte",
                                "Allumage prêt",
                                "Annuler phase d'allumage",
                                "Préchauffage en cours",
                            ],
                            "type": "select",
                            "oid": f"{device_id_prefix}{fct_id_str}/2/1/0",
                            "device_id": self.slugify(f"{self.host}{node_id}"),
                            "device_name": heater_name,
                        }
                    )
                    # Pellet consumption
                    self.devices.append(
                        {
                            "id": self.slugify(f"{self.host}{device_id_prefix}{fct_id_str}/23/100/0"),
                            "name": f"{heater_name} Pellet consumption",
                            "type": "total",
                            "oid": f"{device_id_prefix}{fct_id_str}/23/100/0",
                            "device_id": self.slugify(f"{self.host}{node_id}"),
                            "device_name": heater_name,
                        }
                    )
                    # Total pellet consumption
                    self.devices.append(
                        {
                            "id": self.slugify(f"{self.host}{device_id_prefix}{fct_id_str}/23/103/0"),
                            "name": f"{heater_name} Total Pellet consumption",
                            "type": "total_increasing",
                            "oid": f"{device_id_prefix}{fct_id_str}/23/103/0",
                            "device_id": self.slugify(f"{self.host}{node_id}"),
                            "device_name": heater_name,
                        }
                    )
                    # Running time until stage 1 cleaning
                    self.devices.append(
                        {
                            "id": self.slugify(f"{self.host}{device_id_prefix}{fct_id_str}/20/61/0"),
                            "name": f"{heater_name} Running time until stage 1 cleaning",
                            "type": "sensor",
                            "device_class": "duration",
                            "state_class": None,
                            "unit": "h",
                            "oid": f"{device_id_prefix}{fct_id_str}/20/61/0",
                            "device_id": self.slugify(f"{self.host}{node_id}"),
                            "device_name": heater_name,
                        }
                    )
                    # Running time until stage 2 cleaning
                    self.devices.append(
                        {
                            "id": self.slugify(f"{self.host}{device_id_prefix}{fct_id_str}/20/62/0"),
                            "name": f"{heater_name} Running time until stage 2 cleaning",
                            "type": "sensor",
                            "device_class": "duration",
                            "state_class": None,
                            "unit": "h",
                            "oid": f"{device_id_prefix}{fct_id_str}/20/62/0",
                            "device_id": self.slugify(f"{self.host}{node_id}"),
                            "device_name": heater_name,
                        }
                    )

            # (Optional) one-time temp scan to discover Storage/DHW OIDs
            if SCAN_TEMP_CANDIDATES and not self._did_temp_scan:
                try:
                    await scan_temp_candidates_for_nodes(self)
                except Exception:
                    pass
                self._did_temp_scan = True

        # -------------------------- Read all OIDs ------------------------------
        ret = {
            "devices": self.devices,
            "oids": {},
        }

        # Preserve original semantics: keep raw "value" unless sentinel, else None
        for oid in self.oids:
            try:
                json = await self.fetch(oid)
                if "value" in json and not _is_missing_or_sentinel(json.get("value")):
                    ret["oids"][oid] = json.get("value")
                else:
                    ret["oids"][oid] = None
                    _LOGGER.debug("Invalid or missing value for OID %s: %s", oid, json)
            except Exception as e:
                ret["oids"][oid] = None
                _LOGGER.error("Error while fetching OID %s: %s", oid, str(e))

        return ret
