"""Centrum tabletów: serwer TCP (połączenia tabletów), UDP (odnajdywanie centrum),
rozsyłanie wiadomości głosowych i komunikatów, przekazywanie gier, sterowanie
ładowaniem i licznik energii. Protokół: protocol.py.
"""
import asyncio
import json
import logging
import time
from collections import deque
from datetime import datetime

from . import audio
from .protocol import STATUS_SECONDS, TIMEOUT_SECONDS, ProtocolError, encode, read_frame
from .store import ConfigError

_LOG = logging.getLogger(__name__)

VERSION = "0.1.1"
HELLO_TIMEOUT = 15
OFFLINE_POWER_ON = 15 * 60      # s bez połączenia: włącz zasilanie tabletu (rozładowany?)
SAVE_SECONDS = 300
HISTORY = 30


def now_text() -> str:
    return datetime.now().strftime("%H:%M:%S")


class TabletConn:
    """Jedno połączenie TCP tabletu."""
    def __init__(self, tid, name, ip, reader, writer, hello):
        self.tid, self.name, self.ip = tid, name, ip
        self.reader, self.writer = reader, writer
        self.hello = hello
        self.lock = asyncio.Lock()
        self.last_seen = time.monotonic()

    async def send(self, header: dict, data: bytes | None = None):
        async with self.lock:
            self.writer.write(encode(header, data))
            await asyncio.wait_for(self.writer.drain(), 30)

    def close(self):
        try:
            self.writer.close()
        except Exception:       # noqa: BLE001
            pass


class Discovery(asyncio.DatagramProtocol):
    def __init__(self, center):
        self.center = center
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        try:
            msg = json.loads(data)
        except ValueError:
            return
        if isinstance(msg, dict) and msg.get("type") == "discover":
            reply = {"type": "center", "tcp": self.center.tcp_port, "name": "Kiosk Center",
                     "version": VERSION}
            self.transport.sendto(json.dumps(reply).encode(), addr)


class Center:
    def __init__(self, store, ha, energy, mqtt=None, tcp_port=47700, udp_port=47701):
        self.store, self.ha, self.energy, self.mqtt = store, ha, energy, mqtt
        self.tcp_port, self.udp_port = tcp_port, udp_port
        self.conns: dict[str, TabletConn] = {}
        self.status: dict[str, dict] = {}        # tid -> ostatni status tabletu
        self.offline_since: dict[str, float] = {}
        self.switch_sent: dict[str, tuple] = {}   # tid -> (stan, czas) ostatniego polecenia
        self.history = deque(maxlen=HISTORY)    # ostatnie wiadomości (panel)
        self._server = None
        self._udp = None
        self._tasks = []
        ha.listen(self._on_ha_state)

    # --- start / stop ---------------------------------------------------------------------
    async def start(self):
        self._server = await asyncio.start_server(self._client, "0.0.0.0", self.tcp_port,
                                                  limit=512 * 1024)
        loop = asyncio.get_running_loop()
        self._udp, _ = await loop.create_datagram_endpoint(
            lambda: Discovery(self), local_addr=("0.0.0.0", self.udp_port), allow_broadcast=True)
        self._tasks.append(asyncio.create_task(self._watchdog()))
        now = time.monotonic()
        for tid in self.store.tablets:
            self.offline_since.setdefault(tid, now)
        _LOG.info("Centrum: TCP %d, UDP %d", self.tcp_port, self.udp_port)
        self.publish_all()

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        for c in list(self.conns.values()):
            c.close()
        if self._server:
            self._server.close()
        if self._udp:
            self._udp.close()
        self.store.save_energy()

    # --- stan dla panelu i MQTT -----------------------------------------------------------
    def tablet_view(self, t: dict) -> dict:
        tid = t["id"]
        st = self.status.get(tid, {})
        conn = self.conns.get(tid)
        return {**t, "online": conn is not None, "battery": st.get("battery"),
                "charging": st.get("charging"), "temperature": st.get("temperature"),
                "version": (conn.hello.get("version") if conn else st.get("version")),
                "app": conn.hello.get("app") if conn else None,
                "power": self.energy.power(tid), "energy": self.energy.kwh(tid),
                "switch": self.ha.state(t["switch_entity"]) if t["switch_entity"] else None,
                "last_seen": st.get("time")}

    def publish(self, tid: str):
        t = self.store.tablets.get(tid)
        if not self.mqtt or not t:
            return
        v = self.tablet_view(t)
        self.mqtt.publish_tablet(tid, v["online"], {
            "battery": v["battery"], "charging": bool(v["charging"]),
            "temperature": v["temperature"], "version": v["version"], "ip": t["ip"],
            "power": v["power"], "energy": v["energy"]})

    def publish_all(self, discover=True):
        if not self.mqtt:
            return
        if discover:
            self.mqtt.discover(list(self.store.tablets.values()))
        for tid in self.store.tablets:
            self.publish(tid)
        self.mqtt.publish_online(len(self.conns))

    def _log(self, kind: str, source: str, text: str, report: dict | None = None):
        self.history.appendleft({"time": now_text(), "kind": kind, "from": source, "text": text,
                                 "sent": (report or {}).get("sent", []),
                                 "skipped": (report or {}).get("skipped", [])})

    # --- połączenie tabletu -----------------------------------------------------------------
    async def _client(self, reader, writer):
        ip = (writer.get_extra_info("peername") or ("?",))[0]
        conn = None
        try:
            frame = await asyncio.wait_for(read_frame(reader), HELLO_TIMEOUT)
            if not frame or frame[0].get("type") != "hello":
                raise ProtocolError("pierwsza ramka to nie hello")
            hello = frame[0]
            t = self.store.by_ip(ip) or self.store.by_name(hello.get("name", ""))
            if t is None:               # nowy tablet - dodany sam, do uzupełnienia w panelu
                name = str(hello.get("name") or f"Tablet {ip}")[:40]
                if self.store.by_name(name):
                    name = f"{name} ({ip})"
                t = self.store.add({"name": name, "ip": ip, "model": str(hello.get("model", ""))[:40]})
                _LOG.info("Nowy tablet %s (%s) dodany do konfiguracji", t["name"], ip)
                if self.mqtt:
                    self.mqtt.discover([t])
            elif t["ip"] != ip:         # rozpoznany po nazwie - nowy adres w konfiguracji
                try:
                    t = self.store.update(t["id"], {"ip": ip})
                except ConfigError as e:
                    _LOG.warning("Tablet %s: nowy adres %s nie zapisany: %s", t["name"], ip, e)
            old = self.conns.pop(t["id"], None)
            if old:
                old.close()
            conn = TabletConn(t["id"], t["name"], ip, reader, writer, hello)
            self.conns[t["id"]] = conn
            self.offline_since.pop(t["id"], None)
            self.status.setdefault(t["id"], {})["version"] = hello.get("version")
            _LOG.info("Tablet %s połączony (%s, %s %s)", t["name"], ip, hello.get("app"),
                      hello.get("version"))
            await conn.send({"type": "welcome", "tablet": self._tablet_public(t),
                             "center": {"version": VERSION, "status_seconds": STATUS_SECONDS},
                             "tablets": self.online_names()})
            self.publish(t["id"])
            if self.mqtt:
                self.mqtt.publish_online(len(self.conns))
            while True:
                frame = await asyncio.wait_for(read_frame(reader), TIMEOUT_SECONDS)
                if frame is None:
                    break
                conn.last_seen = time.monotonic()
                await self._handle(conn, *frame)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError) as e:
            _LOG.info("Połączenie %s zakończone: %s", ip, type(e).__name__)
        except ProtocolError as e:
            _LOG.warning("Połączenie %s: błąd protokołu: %s", ip, e)
        except Exception:               # noqa: BLE001
            _LOG.exception("Połączenie %s: błąd", ip)
        finally:
            if conn and self.conns.get(conn.tid) is conn:
                del self.conns[conn.tid]
                self.offline_since[conn.tid] = time.monotonic()
                _LOG.info("Tablet %s rozłączony", conn.name)
                self.publish(conn.tid)
                if self.mqtt:
                    self.mqtt.publish_online(len(self.conns))
            try:
                writer.close()
            except Exception:           # noqa: BLE001
                pass

    @staticmethod
    def _tablet_public(t: dict) -> dict:
        return {k: t[k] for k in ("id", "name", "place", "model", "voice", "audio_format")}

    def online_names(self) -> list:
        return sorted(c.name for c in self.conns.values())

    async def _handle(self, conn: TabletConn, header: dict, data):
        kind = header.get("type")
        req = header.get("req")
        if kind == "ping":
            await conn.send({"type": "pong", "req": req})
        elif kind == "status":
            st = self.status.setdefault(conn.tid, {})
            for key in ("battery", "charging", "temperature", "brightness", "volume", "screen",
                        "lux", "version"):
                if key in header:
                    st[key] = header[key]
            st["time"] = now_text()
            self.publish(conn.tid)
            await self._charge_control(conn.tid)
        elif kind == "get":
            await conn.send({"type": "data", "req": req, "screen": header.get("screen"),
                             "states": self.screen_states(header)})
        elif kind == "tablets":
            await conn.send({"type": "tablets", "req": req, "tablets": [
                {"name": c.name, "place": self.store.tablets[c.tid]["place"]}
                for c in self.conns.values() if c is not conn and c.tid in self.store.tablets]})
        elif kind == "voice":
            if not data:
                raise ProtocolError("wiadomość głosowa bez danych")
            report = await self.send_voice(conn.name, data, header.get("mime", ""),
                                           to=header.get("to"), exclude=conn.tid)
            await conn.send({"type": "result", "req": req, **report})
        elif kind == "text":
            report = await self.send_text(conn.name, str(header.get("text", "")),
                                          to=header.get("to"), exclude=conn.tid)
            await conn.send({"type": "result", "req": req, **report})
        elif kind == "game":
            await self._relay_game(conn, header)
        else:
            _LOG.debug("Tablet %s: nieznany typ %s", conn.name, kind)

    def screen_states(self, header: dict) -> dict:
        """Stany encji jednego ekranu (lista z ustawień albo podana w zapytaniu)."""
        ids = header.get("entities")
        if not isinstance(ids, list):
            ids = self.store.settings.get("screens", {}).get(str(header.get("screen")), [])
        out = {}
        for eid in ids[:200]:
            s = self.ha.states.get(str(eid))
            if s:
                out[eid] = {"state": s["state"], "attributes": s.get("attributes", {}),
                            "last_changed": s.get("last_changed")}
        return out

    # --- gry ---------------------------------------------------------------------------------
    async def _relay_game(self, conn: TabletConn, header: dict):
        msg = {**header, "from": conn.name}
        to = header.get("to")
        targets = [c for c in self.conns.values() if c is not conn
                   and (to in (None, "*", "") or c.name.lower() == str(to).lower())]
        for c in targets:
            try:
                await c.send(msg)
            except Exception as e:      # noqa: BLE001
                _LOG.warning("Gra: nie dostarczono do %s: %s", c.name, e)

    # --- wiadomości ---------------------------------------------------------------------------
    def _recipients(self, to, exclude=None, force=False):
        """(lista połączeń, pominięte [(nazwa, powód)]). to: None/"*"/lista id lub nazw."""
        if isinstance(to, str) and to not in ("", "*"):
            to = [to]
        explicit = isinstance(to, list) and len(to) > 0
        wanted = {str(x).lower() for x in to} if explicit else None
        cond_entity = self.store.settings.get("voice_condition_entity")
        cond_state = self.store.settings.get("voice_condition_state") or "on"
        cond_ok = force or not cond_entity or self.ha.state(cond_entity) == cond_state
        targets, skipped = [], []
        for tid, t in self.store.tablets.items():
            if tid == exclude:
                continue
            if explicit and tid not in wanted and t["name"].lower() not in wanted:
                continue
            if not explicit and not t["voice"]:
                skipped.append((t["name"], "odczytywanie wyłączone"))
                continue
            if not cond_ok:
                skipped.append((t["name"], f"warunek: {cond_entity} nie jest {cond_state}"))
                continue
            conn = self.conns.get(tid)
            if conn is None:
                skipped.append((t["name"], "niedostępny"))
                continue
            targets.append(conn)
        return targets, skipped

    async def _deliver(self, targets, header, converted: audio.Converted | None):
        sent, skipped = [], []

        async def one(conn):
            t = self.store.tablets.get(conn.tid, {})
            fmt = t.get("audio_format", "m4a")
            try:
                if converted is not None:
                    data, duration = await converted.get(fmt)
                    await conn.send({**header, "format": fmt, "duration": duration}, data)
                else:
                    await conn.send(header)
                sent.append(conn.name)
            except Exception as e:      # noqa: BLE001
                _LOG.warning("Nie dostarczono do %s: %s", conn.name, e)
                skipped.append((conn.name, f"błąd: {e}"))
        await asyncio.gather(*(one(c) for c in targets))
        return sent, skipped

    async def send_voice(self, source: str, data: bytes, mime: str, to=None, exclude=None,
                         force=False) -> dict:
        targets, skipped = self._recipients(to, exclude, force)
        conv = audio.Converted(data, audio.ext_for(mime))
        header = {"type": "voice", "from": source, "time": now_text()}
        sent, errors = await self._deliver(targets, header, conv)
        report = {"ok": bool(sent), "sent": sent, "skipped": skipped + errors}
        self._log("voice", source, f"nagranie ({len(data) // 1024} kB)", report)
        _LOG.info("Wiadomość głosowa od %s: %s", source, report)
        return report

    async def send_text(self, source: str, text: str, to=None, exclude=None, force=False) -> dict:
        text = text.strip()[:500]
        if not text:
            return {"ok": False, "error": "pusty komunikat", "sent": [], "skipped": []}
        targets, skipped = self._recipients(to, exclude, force)
        conv = None
        if targets:
            s = self.store.settings
            try:
                speech, ext = await self.ha.tts_audio(text, s.get("tts_engine"), s.get("tts_language"))
                conv = audio.Converted(speech, ext)
            except Exception as e:      # noqa: BLE001 - tablet przeczyta sam albo pokaże tekst
                _LOG.warning("Mowa dla komunikatu niedostępna: %s", e)
        header = {"type": "text", "from": source, "time": now_text(), "text": text}
        sent, errors = await self._deliver(targets, header, conv)
        report = {"ok": bool(sent), "sent": sent, "skipped": skipped + errors}
        self._log("text", source, text, report)
        _LOG.info("Komunikat od %s: %r %s", source, text, report)
        return report

    async def push_config(self, tid: str):
        conn = self.conns.get(tid)
        t = self.store.tablets.get(tid)
        if conn and t:
            conn.name = t["name"]
            try:
                await conn.send({"type": "config", "tablet": self._tablet_public(t)})
            except Exception:           # noqa: BLE001
                pass

    def drop(self, tid: str):
        conn = self.conns.pop(tid, None)
        if conn:
            conn.close()
        self.status.pop(tid, None)
        self.offline_since.pop(tid, None)

    # --- ładowanie i energia -----------------------------------------------------------------
    async def _switch(self, tid: str, on: bool, reason: str):
        t = self.store.tablets.get(tid)
        if not t or not t["switch_entity"]:
            return
        want = "on" if on else "off"
        if self.ha.state(t["switch_entity"]) == want:
            return
        last = self.switch_sent.get(tid)
        if last and last[0] == want and time.monotonic() - last[1] < 120:
            return                      # polecenie już wysłane, HA jeszcze nie zmienił stanu
        self.switch_sent[tid] = (want, time.monotonic())
        domain = t["switch_entity"].split(".")[0]
        try:
            await self.ha.call(domain, f"turn_{want}", {"entity_id": t["switch_entity"]})
            _LOG.info("Tablet %s: zasilanie %s (%s)", t["name"], want, reason)
        except Exception as e:          # noqa: BLE001
            _LOG.warning("Tablet %s: nie udało się przełączyć %s: %s", t["name"],
                         t["switch_entity"], e)

    async def _charge_control(self, tid: str):
        t = self.store.tablets.get(tid)
        battery = self.status.get(tid, {}).get("battery")
        if not t or not t["switch_entity"] or not isinstance(battery, (int, float)):
            return
        if t["charge_on_below"] and battery <= t["charge_on_below"]:
            await self._switch(tid, True, f"bateria {battery}%")
        elif t["charge_off_above"] and battery >= t["charge_off_above"]:
            await self._switch(tid, False, f"bateria {battery}%")

    def _on_ha_state(self, entity_id, new_state):
        self.energy.on_state(entity_id, new_state)
        for tid in list(self.energy.changed):
            self.publish(tid)
        self.energy.changed.clear()

    async def _watchdog(self):
        last_save = time.monotonic()
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            for conn in list(self.conns.values()):
                if now - conn.last_seen > TIMEOUT_SECONDS:
                    _LOG.info("Tablet %s nie odpowiada - rozłączam", conn.name)
                    conn.close()
            for tid, since in list(self.offline_since.items()):
                if tid in self.store.tablets and now - since > OFFLINE_POWER_ON:
                    await self._switch(tid, True, "brak połączenia")
            self.energy.tick()
            for tid in list(self.energy.changed):
                self.publish(tid)
            self.energy.changed.clear()
            if now - last_save > SAVE_SECONDS:
                self.store.save_energy()
                last_save = now
