"""Konfiguracja Kiosk Center (tablety i ustawienia ogólne) w /data/config.json oraz
liczniki energii ładowania w /data/energy.json. Zapis atomowy (plik tymczasowy + rename).
"""
import ipaddress
import json
import logging
import os
import re
import unicodedata
from pathlib import Path

_LOG = logging.getLogger(__name__)

AUDIO_FORMATS = {               # format pliku dla tabletu -> opis w panelu
    "m4a": "M4A (AAC) - Android, iOS",
    "mp3": "MP3",
    "ogg": "OGG (Vorbis)",
    "opus": "Opus",
    "wav": "WAV (PCM)",
}

DEFAULT_SETTINGS = {
    # komunikaty głosowe odtwarzane tylko, gdy ta encja ma podany stan (np. ktoś jest w domu)
    "voice_condition_entity": "switch.main_power_switch",
    "voice_condition_state": "on",
    "tts_engine": "tts.home_assistant_cloud",
    "tts_language": "pl-PL",
    "quick_messages": ["Obiad gotowy", "Wracam do domu", "Kolacja", "Czas spać"],
    # ekran tabletu -> encje HA, które tablet dostaje jednym komunikatem "get"
    "screens": {},
}

DEFAULT_TABLET = {
    "name": "",
    "ip": "",
    "place": "",
    "model": "",
    "voice": True,              # odczytywanie komunikatów głosowych (wysyłka do wszystkich)
    "audio_format": "m4a",
    "power_entity": "",         # opcjonalnie: moc gniazdka ładowarki (W)
    "energy_entity": "",        # opcjonalnie: licznik energii gniazdka (kWh albo Wh)
    "switch_entity": "",        # opcjonalnie: włącznik zasilania (ładowarki) tabletu
    "charge_on_below": 30,      # % baterii: włącz ładowanie (0 - bez sterowania)
    "charge_off_above": 90,     # % baterii: wyłącz ładowanie (0 - bez sterowania)
}

ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")


def slug(text: str) -> str:
    """Nazwa -> identyfikator ASCII (encje MQTT, tematy)."""
    text = text.replace("ł", "l").replace("Ł", "L")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "tablet"


class ConfigError(ValueError):
    """Błędne dane z panelu - komunikat dla użytkownika."""


def _atomic_write(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class Store:
    def __init__(self, data_dir: str):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "config.json"
        self.energy_path = self.dir / "energy.json"
        self.settings = json.loads(json.dumps(DEFAULT_SETTINGS))
        self.tablets: dict[str, dict] = {}
        self.energy: dict[str, dict] = {}
        self._load()

    # --- odczyt / zapis -------------------------------------------------------------
    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.settings.update(data.get("settings", {}))
            for tid, t in data.get("tablets", {}).items():
                self.tablets[tid] = {**DEFAULT_TABLET, **t, "id": tid}
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            _LOG.error("Nie udało się wczytać %s: %s", self.path, e)
        try:
            self.energy = json.loads(self.energy_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.energy = {}

    def save(self):
        _atomic_write(self.path, {"settings": self.settings, "tablets": self.tablets})

    def save_energy(self):
        _atomic_write(self.energy_path, self.energy)

    # --- tablety ----------------------------------------------------------------------
    def by_ip(self, ip: str):
        return next((t for t in self.tablets.values() if t["ip"] == ip), None)

    def by_name(self, name: str):
        name = (name or "").strip().lower()
        return next((t for t in self.tablets.values() if t["name"].lower() == name), None)

    def _clean(self, data: dict, current_id=None) -> dict:
        t = {k: data.get(k, DEFAULT_TABLET[k]) for k in DEFAULT_TABLET}
        t["name"] = str(t["name"]).strip()
        t["ip"] = str(t["ip"]).strip()
        t["place"] = str(t["place"]).strip()
        t["model"] = str(t["model"]).strip()
        t["voice"] = bool(t["voice"])
        if not t["name"]:
            raise ConfigError("Podaj nazwę tabletu")
        try:
            ipaddress.ip_address(t["ip"])
        except ValueError:
            raise ConfigError(f"Błędny adres IP: {t['ip'] or '(pusty)'}") from None
        if t["audio_format"] not in AUDIO_FORMATS:
            raise ConfigError(f"Nieznany format dźwięku: {t['audio_format']}")
        for key in ("power_entity", "energy_entity", "switch_entity"):
            t[key] = str(t[key] or "").strip()
            if t[key] and not ENTITY_RE.match(t[key]):
                raise ConfigError(f"Błędny identyfikator encji: {t[key]}")
        for key in ("charge_on_below", "charge_off_above"):
            try:
                t[key] = max(0, min(100, int(t[key] or 0)))
            except (TypeError, ValueError):
                raise ConfigError("Progi ładowania to liczby 0-100") from None
        if t["charge_on_below"] and t["charge_off_above"] \
                and t["charge_on_below"] >= t["charge_off_above"]:
            raise ConfigError("Próg włączenia ładowania musi być niższy niż próg wyłączenia")
        for other in self.tablets.values():
            if other["id"] == current_id:
                continue
            if other["ip"] == t["ip"]:
                raise ConfigError(f"Adres {t['ip']} ma już tablet {other['name']}")
            if other["name"].lower() == t["name"].lower():
                raise ConfigError(f"Tablet o nazwie {t['name']} już jest")
        return t

    def add(self, data: dict) -> dict:
        t = self._clean(data)
        tid = base = slug(t["name"])
        n = 2
        while tid in self.tablets:
            tid, n = f"{base}_{n}", n + 1
        t["id"] = tid
        self.tablets[tid] = t
        self.save()
        return t

    def update(self, tid: str, data: dict) -> dict:
        if tid not in self.tablets:
            raise ConfigError("Nie ma takiego tabletu")
        t = self._clean({**self.tablets[tid], **data}, current_id=tid)
        t["id"] = tid               # identyfikator stały - encje w HA zostają te same
        self.tablets[tid] = t
        self.save()
        return t

    def delete(self, tid: str) -> dict:
        t = self.tablets.pop(tid, None)
        if t is None:
            raise ConfigError("Nie ma takiego tabletu")
        self.energy.pop(tid, None)
        self.save()
        self.save_energy()
        return t

    # --- ustawienia ogólne --------------------------------------------------------------
    def update_settings(self, data: dict) -> dict:
        s = dict(self.settings)
        if "voice_condition_entity" in data:
            e = str(data["voice_condition_entity"] or "").strip()
            if e and not ENTITY_RE.match(e):
                raise ConfigError(f"Błędny identyfikator encji: {e}")
            s["voice_condition_entity"] = e
        for key in ("voice_condition_state", "tts_engine", "tts_language"):
            if key in data:
                s[key] = str(data[key] or "").strip()
        if "quick_messages" in data:
            msgs = data["quick_messages"]
            if isinstance(msgs, str):
                msgs = msgs.splitlines()
            s["quick_messages"] = [str(m).strip() for m in msgs if str(m).strip()][:20]
        if "screens" in data:
            screens = data["screens"]
            if not isinstance(screens, dict) or not all(
                    isinstance(v, list) and all(ENTITY_RE.match(str(e)) for e in v)
                    for v in screens.values()):
                raise ConfigError("Ekrany: słownik nazwa ekranu -> lista encji")
            s["screens"] = screens
        self.settings = s
        self.save()
        return s
