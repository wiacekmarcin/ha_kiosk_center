"""Połączenie z Home Assistant: WebSocket (stany, zmiany stanów, usługi) i REST (mowa TTS).

W aplikacji HA: adres http://supervisor/core i token SUPERVISOR_TOKEN (homeassistant_api).
Na komputerze (testy): zmienne HA_URL i HA_TOKEN.
"""
import asyncio
import logging
import os

import aiohttp

_LOG = logging.getLogger(__name__)


class HAError(Exception):
    pass


class HAClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session
        self.base = os.environ.get("HA_URL", "http://supervisor/core").rstrip("/")
        self.token = os.environ.get("HA_TOKEN") or os.environ.get("SUPERVISOR_TOKEN", "")
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.states: dict[str, dict] = {}       # entity_id -> stan (jak w /api/states)
        self.connected = False
        self._ws = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._listeners = []                    # fn(entity_id, new_state | None)
        self._task = None
        self._ready = asyncio.Event()

    def listen(self, fn):
        self._listeners.append(fn)

    async def start(self):
        self._task = asyncio.create_task(self._run())
        try:
            await asyncio.wait_for(self._ready.wait(), 20)
        except asyncio.TimeoutError:
            _LOG.warning("HA: brak połączenia po 20 s - łączę dalej w tle")

    async def stop(self):
        if self._task:
            self._task.cancel()
        if self._ws is not None:
            await self._ws.close()

    # --- WebSocket ----------------------------------------------------------------------
    async def _run(self):
        url = self.base.replace("http", "ws", 1) + ("/websocket" if self.base.endswith("/core")
                                                    else "/api/websocket")
        delay = 2
        while True:
            try:
                async with self.session.ws_connect(url, heartbeat=30, max_msg_size=0) as ws:
                    self._ws = ws
                    msg = await ws.receive_json()
                    if msg.get("type") != "auth_required":
                        raise HAError(f"nieoczekiwana odpowiedź: {msg}")
                    await ws.send_json({"type": "auth", "access_token": self.token})
                    msg = await ws.receive_json()
                    if msg.get("type") != "auth_ok":
                        raise HAError(f"logowanie odrzucone: {msg.get('message', msg)}")
                    reader = asyncio.create_task(self._reader(ws))
                    states = await self.command({"type": "get_states"})
                    self.states = {s["entity_id"]: s for s in states}
                    await self.command({"type": "subscribe_events", "event_type": "state_changed"})
                    self.connected = True
                    delay = 2
                    self._ready.set()
                    _LOG.info("HA: połączono, %d encji", len(self.states))
                    for fn in self._listeners:
                        fn(None, None)          # pełne odświeżenie po (ponownym) połączeniu
                    await reader
            except asyncio.CancelledError:
                raise
            except Exception as e:      # noqa: BLE001 - restart HA, sieć
                _LOG.warning("HA: %s - ponowne połączenie za %d s", e, delay)
            self.connected = False
            self._ws = None
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(HAError("rozłączono"))
            self._pending.clear()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def _reader(self, ws):
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = msg.json()
            if data.get("type") == "event":
                ev = data["event"]
                if ev.get("event_type") == "state_changed":
                    eid = ev["data"]["entity_id"]
                    new = ev["data"].get("new_state")
                    if new is None:
                        self.states.pop(eid, None)
                    else:
                        self.states[eid] = new
                    for fn in self._listeners:
                        try:
                            fn(eid, new)
                        except Exception:       # noqa: BLE001
                            _LOG.exception("HA: błąd obsługi zmiany %s", eid)
            elif data.get("type") == "result":
                fut = self._pending.pop(data.get("id"), None)
                if fut and not fut.done():
                    if data.get("success"):
                        fut.set_result(data.get("result"))
                    else:
                        fut.set_exception(HAError(data.get("error", {}).get("message", "błąd")))

    async def command(self, payload: dict, timeout=30):
        if self._ws is None:
            raise HAError("brak połączenia z HA")
        self._id += 1
        payload = {**payload, "id": self._id}
        fut = asyncio.get_running_loop().create_future()
        self._pending[self._id] = fut
        await self._ws.send_json(payload)
        return await asyncio.wait_for(fut, timeout)

    # --- pomocnicze -----------------------------------------------------------------------
    def state(self, entity_id: str):
        s = self.states.get(entity_id)
        return s["state"] if s else None

    def number(self, entity_id: str):
        try:
            return float(self.state(entity_id))
        except (TypeError, ValueError):
            return None

    async def call(self, domain: str, service: str, data: dict):
        return await self.command({"type": "call_service", "domain": domain,
                                   "service": service, "service_data": data})

    async def tts_audio(self, text: str, engine: str, language: str) -> tuple[bytes, str]:
        """Mowa z silnika TTS HA: (dane, rozszerzenie)."""
        payload = {"engine_id": engine, "message": text}
        if language:
            payload["language"] = language
        async with self.session.post(f"{self.base}/api/tts_get_url", json=payload,
                                     headers=self.headers, timeout=aiohttp.ClientTimeout(total=40)) as r:
            if r.status != 200:
                raise HAError(f"tts_get_url: HTTP {r.status} {await r.text()}")
            info = await r.json()
        path = info.get("path") or "/" + info["url"].split("/", 3)[3]
        async with self.session.get(f"{self.base}{path}", headers=self.headers,
                                    timeout=aiohttp.ClientTimeout(total=40)) as r:
            if r.status != 200:
                raise HAError(f"pobranie mowy: HTTP {r.status}")
            data = await r.read()
        ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else "mp3"
        return data, ext
