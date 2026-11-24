"""Encje w Home Assistant przez MQTT discovery (broker Mosquitto z HA).

Każdy tablet to urządzenie w HA z encjami: połączenie, bateria, ładowanie, temperatura
baterii, wersja aplikacji, adres IP, moc i energia ładowania (gdy skonfigurowane) oraz
powiadomienie "Komunikat" (notify.send_message - tekst czytany na tym tablecie).
Urządzenie "Kiosk Center": liczba tabletów online i "Komunikat na wszystkie tablety".

Dane brokera: Supervisor (/services/mqtt), na komputerze zmienne MQTT_HOST, MQTT_PORT,
MQTT_USER, MQTT_PASSWORD. Brak brokera = centrum działa bez encji.
"""
import asyncio
import json
import logging
import os

import aiohttp

_LOG = logging.getLogger(__name__)

PREFIX = "kiosk_center"
DISCOVERY = "homeassistant"
STATUS = f"{PREFIX}/status"
ALL_NOTIFY = f"{PREFIX}/all/notify"


async def broker_info(session: aiohttp.ClientSession) -> dict | None:
    if os.environ.get("MQTT_HOST"):
        return {"host": os.environ["MQTT_HOST"], "port": int(os.environ.get("MQTT_PORT", 1883)),
                "username": os.environ.get("MQTT_USER", ""),
                "password": os.environ.get("MQTT_PASSWORD", "")}
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    try:
        async with session.get("http://supervisor/services/mqtt",
                               headers={"Authorization": f"Bearer {token}"},
                               timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                _LOG.warning("MQTT: Supervisor nie podał brokera (HTTP %s)", r.status)
                return None
            return (await r.json()).get("data")
    except Exception as e:          # noqa: BLE001
        _LOG.warning("MQTT: brak danych brokera: %s", e)
        return None


class MqttBridge:
    def __init__(self, on_notify):
        self.on_notify = on_notify      # fn(tablet_id | None, tekst) - None = wszystkie
        self.client = None
        self.loop = None
        self.connected = False
        self.on_ha_online = None        # fn() - HA wystartował: ponowne discovery

    def start(self, info: dict):
        import paho.mqtt.client as mqtt
        self.loop = asyncio.get_running_loop()
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="kiosk_center")
        except AttributeError:          # paho-mqtt 1.x
            client = mqtt.Client(client_id="kiosk_center")
        if info.get("username"):
            client.username_pw_set(info["username"], info.get("password") or None)
        client.will_set(STATUS, "offline", retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        client.reconnect_delay_set(2, 60)
        client.connect_async(info["host"], int(info.get("port", 1883)), keepalive=60)
        client.loop_start()
        self.client = client

    def stop(self):
        if self.client:
            self.client.publish(STATUS, "offline", retain=True)
            self.client.disconnect()
            self.client.loop_stop()

    # --- wątek paho -> pętla asyncio ------------------------------------------------------
    def _on_connect(self, client, _userdata, _flags, rc, *_args):
        if getattr(rc, "is_failure", rc != 0):
            _LOG.warning("MQTT: odmowa połączenia: %s", rc)
            return
        self.connected = True
        client.subscribe([(f"{PREFIX}/+/notify", 0), (f"{DISCOVERY}/status", 0)])
        client.publish(STATUS, "online", retain=True)
        _LOG.info("MQTT: połączono z brokerem")
        if self.on_ha_online:
            self.loop.call_soon_threadsafe(self.on_ha_online)

    def _on_disconnect(self, *_args):
        self.connected = False

    def _on_message(self, _client, _userdata, msg):
        text = msg.payload.decode("utf-8", errors="replace").strip()
        if msg.topic == f"{DISCOVERY}/status":
            if text == "online" and self.on_ha_online:
                self.loop.call_soon_threadsafe(self.on_ha_online)
            return
        parts = msg.topic.split("/")
        if len(parts) == 3 and parts[2] == "notify" and text:
            tid = None if parts[1] == "all" else parts[1]
            self.loop.call_soon_threadsafe(self.on_notify, tid, text)

    # --- publikacja -----------------------------------------------------------------------
    def _pub(self, topic, payload, retain=False):
        if self.client is None:
            return
        if not isinstance(payload, str):
            payload = json.dumps(payload, ensure_ascii=False)
        self.client.publish(topic, payload, retain=retain)

    @staticmethod
    def _center_device():
        return {"identifiers": [PREFIX], "name": "Kiosk Center", "manufacturer": "Kiosk Center",
                "model": "Centrum tabletów"}

    def _configs(self, t: dict) -> dict:
        tid = t["id"]
        p = f"{PREFIX}/{tid}"
        dev = {"identifiers": [f"{PREFIX}_{tid}"], "name": t["name"], "manufacturer": "Kiosk Center",
               "model": t["model"] or "Tablet", "via_device": PREFIX}
        if t["place"]:
            dev["suggested_area"] = t["place"]
        online = {"availability": [{"topic": STATUS},
                                   {"topic": f"{p}/connected", "payload_available": "ON",
                                    "payload_not_available": "OFF"}],
                  "availability_mode": "all"}
        base = {"device": dev, "state_topic": f"{p}/state"}

        def ent(key, **kw):
            return {"unique_id": f"{PREFIX}_{tid}_{key}", **base, **kw}

        cfg = {
            ("binary_sensor", "connected"): {
                "unique_id": f"{PREFIX}_{tid}_connected", "device": dev, "name": "Połączenie",
                "state_topic": f"{p}/connected", "device_class": "connectivity",
                "availability_topic": STATUS},
            ("sensor", "battery"): ent("battery", name="Bateria", device_class="battery",
                                       unit_of_measurement="%", state_class="measurement",
                                       value_template="{{ value_json.battery }}", **online),
            ("binary_sensor", "charging"): ent(
                "charging", name="Ładowanie", device_class="battery_charging",
                value_template="{{ 'ON' if value_json.charging else 'OFF' }}", **online),
            ("sensor", "temperature"): ent(
                "temperature", name="Temperatura baterii", device_class="temperature",
                unit_of_measurement="°C", state_class="measurement", entity_category="diagnostic",
                value_template="{{ value_json.temperature }}", **online),
            ("sensor", "version"): ent("version", name="Wersja aplikacji", icon="mdi:application",
                                       entity_category="diagnostic",
                                       value_template="{{ value_json.version }}",
                                       availability_topic=STATUS),
            ("sensor", "ip"): ent("ip", name="Adres IP", icon="mdi:ip-network",
                                  entity_category="diagnostic", value_template="{{ value_json.ip }}",
                                  availability_topic=STATUS),
            ("notify", "message"): {"unique_id": f"{PREFIX}_{tid}_message", "device": dev,
                                    "name": "Komunikat", "command_topic": f"{p}/notify",
                                    "availability_topic": STATUS},
            ("sensor", "power"): None, ("sensor", "energy"): None,
        }
        if t["power_entity"] or t["energy_entity"]:
            cfg[("sensor", "energy")] = ent(
                "energy", name="Energia ładowania", device_class="energy", unit_of_measurement="kWh",
                state_class="total_increasing", suggested_display_precision=3,
                value_template="{{ value_json.energy }}", availability_topic=STATUS)
        if t["power_entity"]:
            cfg[("sensor", "power")] = ent(
                "power", name="Moc ładowania", device_class="power", unit_of_measurement="W",
                state_class="measurement", value_template="{{ value_json.power }}",
                availability_topic=STATUS)
        return cfg

    def discover(self, tablets: list):
        dev = self._center_device()
        self._pub(f"{DISCOVERY}/sensor/{PREFIX}/online/config", {
            "unique_id": f"{PREFIX}_online", "device": dev, "name": "Tablety online",
            "icon": "mdi:tablet", "state_topic": f"{PREFIX}/online", "availability_topic": STATUS,
            "unit_of_measurement": "szt.", "state_class": "measurement"}, retain=True)
        self._pub(f"{DISCOVERY}/notify/{PREFIX}/all/config", {
            "unique_id": f"{PREFIX}_all_message", "device": dev,
            "name": "Komunikat na wszystkie tablety", "command_topic": ALL_NOTIFY,
            "availability_topic": STATUS}, retain=True)
        for t in tablets:
            for (component, key), cfg in self._configs(t).items():
                self._pub(f"{DISCOVERY}/{component}/{PREFIX}_{t['id']}/{key}/config",
                          cfg if cfg else "", retain=True)

    def remove(self, t: dict):
        for component, key in self._configs(t):
            self._pub(f"{DISCOVERY}/{component}/{PREFIX}_{t['id']}/{key}/config", "", retain=True)
        self._pub(f"{PREFIX}/{t['id']}/state", "", retain=True)
        self._pub(f"{PREFIX}/{t['id']}/connected", "", retain=True)

    def publish_tablet(self, tid: str, connected: bool, state: dict):
        self._pub(f"{PREFIX}/{tid}/connected", "ON" if connected else "OFF", retain=True)
        self._pub(f"{PREFIX}/{tid}/state", state, retain=True)

    def publish_online(self, count: int):
        self._pub(f"{PREFIX}/online", str(count), retain=True)
