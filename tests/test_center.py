"""Test centrum na komputerze: dwa symulowane tablety (adresy 127.0.0.2 i 127.0.0.3),
prawdziwy Home Assistant (HA_URL, HA_TOKEN - stany, mowa TTS), ffmpeg (FFMPEG).
Bez MQTT. Uruchomienie: python tests/test_center.py (z katalogu repozytorium).
Wynik: CENTER_OK albo CENTER_FAIL z listą błędów.
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "kiosk_center"))
DATA = tempfile.mkdtemp(prefix="kc_data_")
os.environ.update({"WEB_ANY": "1", "DATA_DIR": DATA})
TCP, UDP, WEB = 47710, 47711, 8199

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402

from center import audio  # noqa: E402
from center.energy import EnergyMeter  # noqa: E402
from center.ha import HAClient  # noqa: E402
from center.protocol import encode, read_frame  # noqa: E402
from center.server import Center  # noqa: E402
from center.store import Store  # noqa: E402
from center.web import make_app  # noqa: E402

ERRORS = []


def check(cond, text):
    print(("OK   " if cond else "BŁĄD ") + text)
    if not cond:
        ERRORS.append(text)


class Tablet:
    """Symulowany tablet: połączenie TCP z własnego adresu 127.0.0.x."""
    def __init__(self, name, ip, formats=("m4a", "mp3")):
        self.name, self.ip, self.formats = name, ip, list(formats)
        self.inbox = asyncio.Queue()

    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", TCP, local_addr=(self.ip, 0))
        await self.send({"type": "hello", "name": self.name, "model": "Symulator", "app": "test",
                         "version": "1.0", "formats": self.formats})
        self.welcome = await self.recv()
        self._task = asyncio.create_task(self._loop())

    async def _loop(self):
        while True:
            frame = await read_frame(self.reader)
            if frame is None:
                await self.inbox.put(None)
                return
            await self.inbox.put(frame)

    async def recv(self, timeout=30):
        if hasattr(self, "_task"):
            return await asyncio.wait_for(self.inbox.get(), timeout)
        return await asyncio.wait_for(read_frame(self.reader), timeout)

    async def recv_type(self, kind, timeout=40):
        while True:
            frame = await self.recv(timeout)
            if frame is None or frame[0].get("type") == kind:
                return frame

    async def send(self, header, data=None):
        self.writer.write(encode(header, data))
        await self.writer.drain()


def sample_audio() -> bytes:
    """2 s tonu 440 Hz w WebM/Opus (jak nagranie z przeglądarki)."""
    out = Path(audio.TMP_DIR) / "kc_test_src.webm"
    subprocess.run([audio.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=2", "-c:a", "libopus", str(out)], check=True)
    data = out.read_bytes()
    out.unlink()
    return data


async def main():
    store = Store(DATA)
    async with aiohttp.ClientSession() as session:
        ha = HAClient(session)
        await ha.start()
        check(ha.connected and len(ha.states) > 10, f"połączenie z HA ({len(ha.states)} encji)")
        energy = EnergyMeter(store, ha)
        center = Center(store, ha, energy, None, TCP, UDP)
        await center.start()
        runner = web.AppRunner(make_app(center))
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", WEB).start()
        base = f"http://127.0.0.1:{WEB}"

        # 1. odnajdywanie przez UDP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(3)
        s.sendto(json.dumps({"type": "discover", "name": "x"}).encode(), ("127.0.0.1", UDP))
        reply = await asyncio.get_running_loop().run_in_executor(None, s.recv, 2000)
        check(json.loads(reply).get("tcp") == TCP, "UDP: centrum odpowiada portem TCP")

        # 2. dwa tablety zgłaszają się same
        a, b = Tablet("Test A", "127.0.0.2"), Tablet("Test B", "127.0.0.3")
        await a.connect()
        await b.connect()
        check(a.welcome[0]["type"] == "welcome" and a.welcome[0]["tablet"]["name"] == "Test A",
              "hello -> welcome z konfiguracją tabletu")
        ids = {t["name"]: t["id"] for t in store.tablets.values()}
        check(set(ids) == {"Test A", "Test B"} and store.tablets[ids["Test B"]]["ip"] == "127.0.0.3",
              f"nowe tablety dodane do konfiguracji: {ids}")
        lustro = Tablet("Lustro", "127.0.0.4", formats=("mp3",))
        await lustro.connect()
        lt = store.by_name("Lustro")
        check(lt and lt["ip"] == "127.0.0.4" and lt["audio_format"] == "mp3"
              and lt["voice"], f"Lustro dodane z MP3 i odbiorem komunikatów: {lt}")
        if lt:
            store.update(lt["id"], {"voice": False})

        # 3. status i lista w panelu
        await a.send({"type": "status", "battery": 25, "charging": True, "temperature": 31.5})
        await a.send({"type": "ping", "req": 1})
        await a.recv_type("pong")
        async with session.get(f"{base}/api/state") as r:
            st = await r.json()
        va = next(t for t in st["tablets"] if t["name"] == "Test A")
        check(va["online"] and va["battery"] == 25 and va["charging"] is True, "status tabletu w panelu")
        async with session.get(f"{base}/") as r:
            check(r.status == 200 and "Nagraj wiadomość" in await r.text(), "strona panelu")

        # 4. dane ekranu jednym komunikatem
        cond = store.settings["voice_condition_entity"]
        await a.send({"type": "get", "req": 7, "entities": [cond, "sun.sun"]})
        data = await a.recv_type("data")
        check(data[0]["req"] == 7 and cond in data[0]["states"], "get: stany encji ekranu")

        # 5. wiadomość głosowa z tabletu A (WebM) -> B w M4A
        force_note = ha.state(cond) != store.settings["voice_condition_state"]
        if force_note:
            print(f"UWAGA: {cond} = {ha.state(cond)} - warunek niespełniony, test z force")
        src = sample_audio()
        if force_note:
            rep = await center.send_voice("Test A", src, "audio/webm", exclude=ids["Test A"], force=True)
        else:
            await a.send({"type": "voice", "req": 2, "mime": "audio/webm;codecs=opus"}, src)
            rep = (await a.recv_type("result"))[0]
        check(rep.get("sent") == ["Test B"], f"wiadomość głosowa doręczona: {rep}")
        v = await b.recv_type("voice")
        check(v[0]["format"] == "m4a" and v[1][4:8] == b"ftyp" and 1.5 < v[0]["duration"] < 2.6,
              f"B dostał M4A {len(v[1])} B, {v[0]['duration']} s")

        # 6. komunikat tekstowy z panelu tylko do B - mowa z HA
        async with session.post(f"{base}/api/text", json={"text": "Test centrum tabletów",
                                                          "to": ids["Test B"], "force": True}) as r:
            rep = await r.json()
        check(rep.get("sent") == ["Test B"], f"komunikat z panelu: {rep}")
        t = await b.recv_type("text")
        check(t[0]["text"] == "Test centrum tabletów" and t[1] and t[0].get("format") == "m4a",
              f"B dostał tekst z mową ({len(t[1] or b'')} B)")

        # 7. gra: A -> B
        await a.send({"type": "game", "kind": "invite", "game": "chess", "session": "s1", "to": "Test B"})
        g = await b.recv_type("game")
        check(g[0]["from"] == "Test A" and g[0]["session"] == "s1", "gra przekazana do B")

        # 8. konfiguracja z panelu: błąd, zmiana formatu (push config), wysyłka MP3
        async with session.post(f"{base}/api/tablets", json={"name": "Zły", "ip": "300.1.1.1"}) as r:
            body = await r.json()
            check(r.status == 400 and "IP" in body["error"], f"walidacja: {body.get('error')}")
        ha.states["sensor.charger_9_power"] = {"state": "6.5", "attributes": {"unit_of_measurement": "W"}}
        async with session.put(f"{base}/api/tablets/{ids['Test B']}", json={
                "audio_format": "mp3", "place": "Test",
                "power_entity": "sensor.charger_9_power", "energy_entity": "sensor.charger_9_energy"}) as r:
            check(r.status == 200, "zmiana konfiguracji B")
        c = await b.recv_type("config")
        check(c[0]["tablet"]["audio_format"] == "mp3", "B dostał nową konfigurację")
        ch = await b.recv_type("charger")
        check(ch is not None and ch[0].get("power") == 6.5 and ch[0].get("energy") is not None,
              f"B dostał po TCP moc i energię ładowarki: {ch and ch[0]}")
        async with session.post(f"{base}/api/voice?to={ids['Test B']}&force=1", data=src,
                                headers={"Content-Type": "audio/webm"}) as r:
            rep = await r.json()
        v = await b.recv_type("voice")
        check(v[0]["format"] == "mp3" and (v[1][:3] == b"ID3" or v[1][0] == 0xFF),
              f"nagranie z panelu do B w MP3 ({len(v[1])} B)")

        # 9. energia: licznik gniazdka (przyrosty) i moc (całkowanie)
        tb = store.tablets[ids["Test B"]]
        tb["energy_entity"] = "sensor.test_energy"
        ha.states["sensor.test_energy"] = {"state": "1.000", "attributes": {"unit_of_measurement": "kWh"}}
        energy.update(ids["Test B"])
        ha.states["sensor.test_energy"]["state"] = "1.012"
        energy.update(ids["Test B"])
        check(abs(energy.kwh(ids["Test B"]) - 0.012) < 1e-6, f"energia z licznika: {energy.kwh(ids['Test B'])}")
        ta = store.tablets[ids["Test A"]]
        ta["power_entity"] = "sensor.test_power"
        ha.states["sensor.test_power"] = {"state": "10", "attributes": {"unit_of_measurement": "W"}}
        energy.update(ids["Test A"], now=1000)
        energy.update(ids["Test A"], now=1360)          # 10 W przez 6 min = 0,001 kWh
        check(abs(energy.kwh(ids["Test A"]) - 0.001) < 1e-6, f"energia z mocy: {energy.kwh(ids['Test A'])}")

        # 10. usunięcie tabletu rozłącza go
        async with session.delete(f"{base}/api/tablets/{ids['Test A']}") as r:
            check(r.status == 200, "usunięcie tabletu A")
        check(await a.recv_type("never", timeout=5) is None, "A rozłączony po usunięciu")

        await center.stop()
        await runner.cleanup()
        await ha.stop()
    shutil.rmtree(DATA, ignore_errors=True)


if __name__ == "__main__":         # test_client.py używa klasy Tablet i sample_audio
    asyncio.run(main())
    print("CENTER_FAIL\n" + "\n".join(ERRORS) if ERRORS else "CENTER_OK")
    sys.exit(1 if ERRORS else 0)
