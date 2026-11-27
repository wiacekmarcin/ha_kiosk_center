"""Start Kiosk Center: python3 -m center.

Opcje aplikacji HA: /data/options.json (tcp_port, udp_port, ntp_server, log_level). Na komputerze:
zmienne DATA_DIR, TCP_PORT, UDP_PORT, NTP_SERVER, WEB_PORT, LOG_LEVEL, HA_URL, HA_TOKEN, MQTT_*.
"""
import asyncio
import logging
import os
import signal

import aiohttp
from aiohttp import web

from .energy import EnergyMeter
from .ha import HAClient
from .mqtt import MqttBridge, broker_info
from .options import options
from .server import VERSION, Center
from .store import Store
from .web import make_app


async def main():
    opts = options()
    logging.basicConfig(level=str(opts["log_level"]).upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    log = logging.getLogger("center")
    log.info("Kiosk Center %s", VERSION)
    store = Store(os.environ.get("DATA_DIR", "/data"))
    async with aiohttp.ClientSession() as session:
        ha = HAClient(session)
        energy = EnergyMeter(store, ha)
        center = Center(store, ha, energy, None, int(opts["tcp_port"]), int(opts["udp_port"]),
                        ntp_server=str(opts["ntp_server"]))
        info = await broker_info(session)
        if info:
            mqtt = MqttBridge(on_notify=lambda tid, text: asyncio.ensure_future(
                center.send_text("Home Assistant", text, to=[tid] if tid else None)))
            mqtt.on_ha_online = lambda: center.publish_all()
            center.mqtt = mqtt
            mqtt.start(info)
        else:
            log.warning("Brak brokera MQTT - encje tabletów w HA nie będą tworzone")
        await ha.start()
        await center.start()
        runner = web.AppRunner(make_app(center), access_log=None)
        await runner.setup()
        # port okna = ingress_port z config.yaml
        await web.TCPSite(runner, "0.0.0.0", int(os.environ.get("WEB_PORT", 47702))).start()
        log.info("Okno aplikacji (ingress) na porcie %s", os.environ.get("WEB_PORT", 47702))
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        log.info("Zatrzymywanie")
        await center.stop()
        if center.mqtt:
            center.mqtt.stop()
        await ha.stop()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
