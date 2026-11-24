"""Test kodu tabletu z centrum: VoiceLink i CenterLink z ../ha_kiosk (kiosk/voice.py,
kiosk/center.py) oraz GameNet z ../ha_kiosk_dzieci łączą się z centrum uruchomionym w tym
procesie; drugi tablet symulowany (adres 127.0.0.3). Prawdziwy HA (HA_URL, HA_TOKEN).
Wynik: CLIENT_OK albo CLIENT_FAIL.
"""
import asyncio
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WS = ROOT.parent
sys.path[:0] = [str(ROOT / "kiosk_center"), str(ROOT / "tests"), str(WS / "ha_kiosk"),
                str(WS / "ha_kiosk_dzieci")]
DATA = tempfile.mkdtemp(prefix="kc_data_")
CACHE = tempfile.mkdtemp(prefix="kc_cache_")
os.environ.update({"WEB_ANY": "1", "DATA_DIR": DATA, "KIVY_NO_ARGS": "1", "KIVY_LOG_MODE": "PYTHON"})
TCP, UDP = 47720, 47721

import aiohttp  # noqa: E402
from kivy.clock import Clock  # noqa: E402

from center.energy import EnergyMeter  # noqa: E402
from center.ha import HAClient  # noqa: E402
from center.server import Center  # noqa: E402
from center.store import Store  # noqa: E402
import test_center  # noqa: E402 - symulowany tablet i próbka dźwięku
test_center.TCP = TCP

ERRORS = []
check = test_center.check
SETTINGS = {"url": os.environ["HA_URL"], "token": os.environ["HA_TOKEN"],
            "center": f"127.0.0.1:{TCP}"}


def wait(cond, timeout=30):
    """Zegar Kivy w tym wątku, aż warunek spełniony."""
    end = time.time() + timeout
    while time.time() < end:
        Clock.tick()
        if cond():
            return True
        time.sleep(0.05)
    return False


def main():
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()

    def run(coro, timeout=60):
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)

    async def start_center():
        session = aiohttp.ClientSession()
        ha = HAClient(session)
        await ha.start()
        store = Store(DATA)
        center = Center(store, ha, EnergyMeter(store, ha), None, TCP, UDP)
        await center.start()
        return center
    center = run(start_center())

    from kiosk import voice
    from dzieci.games.net import GameNet
    got = []
    vl = voice.VoiceLink(SETTINGS, "Tablet Test", CACHE, lambda info, path: got.append((info, path)))
    vl.start()
    check(wait(lambda: vl.center.connected), "VoiceLink połączył się z centrum (kiosk/center.py)")
    check(vl.center.tablet.get("name") == "Tablet Test", f"welcome: {vl.center.tablet}")
    check(wait(lambda: any(t.get("version") is not None or True for t in [center.status.get("tablet_test", {})])
               and "tablet_test" in center.status), "status tabletu w centrum")

    b = test_center.Tablet("Test B", "127.0.0.3")
    run(b.connect())
    src = test_center.sample_audio()

    # 1. nagranie z B -> VoiceLink (M4A z centrum, przez on_message)
    rep = run(center.send_voice("Test B", src, "audio/webm", exclude="test_b", force=True))
    check(rep["sent"] == ["Tablet Test"], f"centrum doręczyło do Tablet Test: {rep}")
    check(wait(lambda: got), "VoiceLink: wiadomość głosowa odebrana")
    if got:
        info, path = got[-1]
        check(info["from"] == "Test B" and path.endswith(".m4a") and Path(path).stat().st_size > 1000,
              f"odebrana: {info}, {Path(path).name}")

    # 2. VoiceLink.send przez centrum -> B
    m4a = Path(CACHE) / "nagranie.m4a"
    shutil.copy(got[-1][1], m4a) if got else None
    result = []
    vl.send(str(m4a), 2.0, lambda ok, err: result.append((ok, err)))
    check(wait(lambda: result, 60) and result[0][0], f"VoiceLink.send przez centrum: {result}")
    v = run(b.recv_type("voice"))
    check(v and v[0]["from"] == "Tablet Test", "B dostał nagranie od Tablet Test")

    # 3. komunikat tekstowy (mowa z centrum)
    got.clear()
    run(center.send_text("Home Assistant", "Próba komunikatu", to=["tablet_test"]), 90)
    check(wait(lambda: got, 60) and got[-1][0].get("text") == "Próba komunikatu"
          and Path(got[-1][1]).stat().st_size > 1000, "VoiceLink: komunikat z mową z centrum")

    # 4. gra przez centrum: zaproszenie z B, przyjęcie przez GameNet, ruch
    events = []
    net = GameNet(SETTINGS, "Tablet Test", events.append, center=vl.center)
    run(b.send({"type": "game", "to": "*", "msg": {"type": "invite", "game": "chess", "session": "s9"}}))
    check(wait(lambda: events), "GameNet: zaproszenie przez centrum")
    if events:
        check(events[0]["type"] == "invite" and events[0]["from"] == "Test B", f"zaproszenie: {events[0]}")
        net.accept(events[0])
        g = run(b.recv_type("game"))
        check(g and g[0]["msg"]["type"] == "accept" and g[0]["msg"]["to"] == "Test B"
              and g[0]["from"] == "Tablet Test", f"B dostał accept: {g and g[0]}")
        run(b.send({"type": "game", "to": "Tablet Test", "msg": {"type": "move", "game": "chess",
                                                                 "session": "s9", "to": "Tablet Test",
                                                                 "seq": 1, "move": [6, 4, 4, 4]}}))
        check(wait(lambda: len(events) > 1) and net.mine(events[-1]), "ruch B dotarł i należy do partii")

    vl.stop()
    net.stop()
    run(center.stop())
    shutil.rmtree(DATA, ignore_errors=True)
    shutil.rmtree(CACHE, ignore_errors=True)


main()
ERRORS = test_center.ERRORS
print("CLIENT_FAIL\n" + "\n".join(ERRORS) if ERRORS else "CLIENT_OK")
os._exit(1 if ERRORS else 0)
