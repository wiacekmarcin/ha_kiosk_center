# ha_kiosk_center

Repozytorium aplikacji Home Assistant **Kiosk Center** - centrum tabletów kiosku:
konfiguracja tabletów, wiadomości głosowe i komunikaty (konwersja dźwięku ffmpeg, mowa
z silnika TTS HA), serwer gier między tabletami, bateria, sterowanie ładowaniem
i energia ładowania. Opis działania: [kiosk_center/DOCS.md](kiosk_center/DOCS.md).

## Instalacja w Home Assistant

1. Ustawienia → Aplikacje → Sklep z aplikacjami → menu ⋮ → Repozytoria.
2. Dodaj adres `https://github.com/wiacekmarcin/ha_kiosk_center`.
3. Na liście pojawi się **Kiosk Center** → Zainstaluj → Uruchom.
4. W pasku bocznym pojawi się okno **Tablety**.

## Budowa katalogu

- `repository.yaml` - opis repozytorium aplikacji.
- `kiosk_center/` - aplikacja: `config.yaml`, `Dockerfile` (Alpine, Python, ffmpeg),
  kod w `center/` (start: `python3 -m center`).
- `tests/test_center.py` - test na komputerze z symulowanymi tabletami i prawdziwym HA.

## Test na komputerze

```bash
export HA_URL=http://192.168.10.250:8123 HA_TOKEN=<token>
python tests/test_center.py      # wymaga aiohttp i ffmpeg (FFMPEG=ścieżka)
```
