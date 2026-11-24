"""Energia zużyta przez tablety na ładowanie (kWh, licznik rosnący - panel Energia HA).

Źródło na tablet (konfiguracja):
- energy_entity: licznik energii gniazdka ładowarki - dodawane są przyrosty licznika
  (spadek licznika = wyzerowanie gniazdka, nowa baza bez dodawania);
- power_entity (gdy brak licznika): moc gniazdka całkowana w czasie (metoda lewych
  prostokątów, przy każdej zmianie mocy i co minutę).
Stan licznika w store.energy (plik /data/energy.json), zapisywany okresowo.
"""
import logging
import time

_LOG = logging.getLogger(__name__)

MAX_GAP = 600           # s - dłuższa przerwa w odczytach mocy nie jest całkowana
MAX_STEP_KWH = 0.5      # większy jednorazowy przyrost licznika = błąd / zmiana licznika


def _kwh(state: dict | None):
    """Stan licznika energii w kWh (Wh przeliczane) albo None."""
    if not state:
        return None
    try:
        value = float(state["state"])
    except (KeyError, TypeError, ValueError):
        return None
    unit = (state.get("attributes", {}).get("unit_of_measurement") or "kWh").strip()
    return value / 1000 if unit == "Wh" else value


class EnergyMeter:
    def __init__(self, store, ha):
        self.store = store
        self.ha = ha
        self.changed = set()            # tablety ze zmienioną energią (do publikacji)

    def _rec(self, tid: str) -> dict:
        return self.store.energy.setdefault(
            tid, {"kwh": 0.0, "last_energy": None, "last_power": None, "last_ts": None})

    def kwh(self, tid: str) -> float:
        return round(self._rec(tid)["kwh"], 4)

    def power(self, tid: str):
        t = self.store.tablets.get(tid)
        return self.ha.number(t["power_entity"]) if t and t["power_entity"] else None

    def on_state(self, entity_id, new_state):
        """Zmiana stanu w HA (entity_id None = pełne odświeżenie po połączeniu)."""
        for tid, t in self.store.tablets.items():
            if entity_id is None or entity_id in (t["energy_entity"], t["power_entity"]):
                self.update(tid)

    def update(self, tid: str, now: float | None = None):
        t = self.store.tablets.get(tid)
        if not t:
            return
        rec = self._rec(tid)
        now = now if now is not None else time.time()
        if t["energy_entity"]:
            value = _kwh(self.ha.states.get(t["energy_entity"]))
            if value is None:
                return
            last = rec["last_energy"]
            if last is not None and 0 < value - last <= MAX_STEP_KWH:
                rec["kwh"] += value - last
                self.changed.add(tid)
            rec["last_energy"] = value
        elif t["power_entity"]:
            watts = self.ha.number(t["power_entity"])
            if rec["last_power"] is not None and rec["last_ts"] is not None:
                dt = now - rec["last_ts"]
                if 0 < dt <= MAX_GAP and rec["last_power"] > 0:
                    rec["kwh"] += rec["last_power"] * dt / 3600 / 1000
                    self.changed.add(tid)
            rec["last_power"] = watts if watts is not None and watts >= 0 else None
            rec["last_ts"] = now
        self.changed.add(tid)

    def tick(self):
        """Co minutę - całkowanie mocy także bez zmian stanu."""
        for tid, t in self.store.tablets.items():
            if t["power_entity"] and not t["energy_entity"]:
                self.update(tid)
