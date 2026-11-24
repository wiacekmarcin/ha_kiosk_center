"""Okno aplikacji w HA (ingress): strona panelu i API JSON.

Dostęp tylko przez ingress HA (adres 172.30.32.2 - proxy Supervisora, użytkownik
zalogowany w HA); na komputerze (testy) zmienna WEB_ANY=1 wyłącza to sprawdzenie.
"""
import logging
import time
import os
from pathlib import Path

from aiohttp import web

from .store import AUDIO_FORMATS, ConfigError

_LOG = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / "static"
INGRESS_IP = "172.30.32.2"
MAX_UPLOAD = 20 * 1024 * 1024


@web.middleware
async def only_ingress(request, handler):
    if not os.environ.get("WEB_ANY") and request.remote not in (INGRESS_IP, "127.0.0.1"):
        return web.Response(status=403, text="Tylko przez Home Assistant")
    try:
        return await handler(request)
    except ConfigError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)


def _targets(value):
    if not value or value in ("*", "all"):
        return None
    return [v for v in (value if isinstance(value, list) else str(value).split(",")) if v]


def make_app(center) -> web.Application:
    store = center.store
    app = web.Application(middlewares=[only_ingress], client_max_size=MAX_UPLOAD)
    routes = web.RouteTableDef()

    @routes.get("/")
    async def index(_request):
        return web.FileResponse(STATIC / "index.html",
                                headers={"Cache-Control": "no-cache"})

    @routes.get("/api/state")
    async def state(_request):
        return web.json_response({
            "version": center_version(),
            "settings": store.settings,
            "tablets": [center.tablet_view(t) for t in store.tablets.values()],
            "formats": AUDIO_FORMATS,
            "history": list(center.history),
            "ha": center.ha.connected,
            "mqtt": bool(center.mqtt and center.mqtt.connected),
            "condition": center.ha.state(store.settings.get("voice_condition_entity") or ""),
        })

    @routes.get("/api/entities")
    async def entities(request):
        domains = tuple(d + "." for d in request.query.get("domain", "").split(",") if d)
        out = [{"id": eid, "name": s.get("attributes", {}).get("friendly_name", eid),
                "unit": s.get("attributes", {}).get("unit_of_measurement", "")}
               for eid, s in sorted(center.ha.states.items())
               if not domains or eid.startswith(domains)]
        return web.json_response(out)

    @routes.post("/api/tablets")
    async def add_tablet(request):
        t = store.add(await request.json())
        if center.mqtt:
            center.mqtt.discover([t])
        center.offline_since.setdefault(t["id"], time.monotonic())
        center.publish(t["id"])
        return web.json_response({"ok": True, "tablet": t})

    @routes.put("/api/tablets/{tid}")
    async def update_tablet(request):
        tid = request.match_info["tid"]
        t = store.update(tid, await request.json())
        if center.mqtt:
            center.mqtt.discover([t])
        center.publish(tid)
        await center.push_config(tid)
        return web.json_response({"ok": True, "tablet": t})

    @routes.delete("/api/tablets/{tid}")
    async def delete_tablet(request):
        t = store.delete(request.match_info["tid"])
        center.drop(t["id"])
        if center.mqtt:
            center.mqtt.remove(t)
            center.mqtt.publish_online(len(center.conns))
        return web.json_response({"ok": True})

    @routes.put("/api/settings")
    async def settings(request):
        return web.json_response({"ok": True, "settings": store.update_settings(await request.json())})

    @routes.post("/api/voice")
    async def voice(request):
        data = await request.read()
        if len(data) < 200:
            raise ConfigError("Nagranie jest puste")
        report = await center.send_voice(
            request.query.get("from") or "Home Assistant", data,
            request.headers.get("Content-Type", ""), to=_targets(request.query.get("to")),
            force=request.query.get("force") == "1")
        return web.json_response(report)

    @routes.post("/api/text")
    async def text(request):
        body = await request.json()
        report = await center.send_text(str(body.get("from") or "Home Assistant"),
                                        str(body.get("text", "")), to=_targets(body.get("to")),
                                        force=bool(body.get("force")))
        return web.json_response(report)

    routes.static("/static", STATIC)
    app.add_routes(routes)
    return app


def center_version():
    from .server import VERSION
    return VERSION
