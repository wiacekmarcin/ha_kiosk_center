"""Protokół tablet <-> Kiosk Center.

Odnalezienie centrum (UDP, port udp_port): tablet wysyła rozgłoszeniem
    {"type": "discover", "name": "<nazwa tabletu>"}
centrum odpowiada na adres nadawcy:
    {"type": "center", "tcp": <port TCP>, "name": "Kiosk Center", "version": "..."}

Połączenie (TCP, port tcp_port) - ramki:
    linia JSON zakończona \\n (nagłówek), a gdy nagłówek ma pole "size": N,
    zaraz za nim N bajtów danych (nagranie, mowa). Pole "size" ustawia encode().

Tablet -> centrum:
    hello   {name, model, app, version, formats}      pierwsza ramka, odpowiedź: welcome;
                                                      nowy tablet dostaje pierwszy obsługiwany format
    status  {battery, charging, temperature, ...}     co STATUS_SECONDS s i po zmianie
    ping                                             odpowiedź: pong
    get     {screen | entities, req}                 odpowiedź: data {screen, req, states}
    voice   {to?, mime} + bajty nagrania              wiadomość głosowa do innych tabletów
    text    {to?, text}                              komunikat tekstowy (centrum: mowa)
    game    {to, game, session, kind, ...}           gra przez sieć - przekazywana dalej
    tablets                                          odpowiedź: tablets {list}
Centrum -> tablet:
    welcome {tablet, center {ntp_server}, tablets}, pong, data, tablets,
    voice   {from, time, duration, format} + bajty w formacie tabletu
    text    {from, time, text, format?} (+ bajty mowy, gdy jest)
    game    {from, ...}                              od drugiego tabletu
    config  {tablet}                                 zmiana konfiguracji w panelu
    charger {power, energy}                          moc ładowarki (W) i energia ładowania (kWh)
                                                     z encji tabletu w panelu (null = brak);
                                                     po połączeniu, po zmianie wartości i konfiguracji
    result  {req, ok, error?, sent, skipped}         wynik voice / text
"""
import json

MAX_HEADER = 256 * 1024             # bajtów w linii nagłówka
MAX_DATA = 20 * 1024 * 1024         # bajtów danych w jednej ramce (nagranie)
STATUS_SECONDS = 30                 # jak często tablet wysyła status
TIMEOUT_SECONDS = 95                # cisza dłuższa niż to = tablet niedostępny


class ProtocolError(Exception):
    pass


def encode(header: dict, data: bytes | None = None) -> bytes:
    header = dict(header)
    if data:
        header["size"] = len(data)
    else:
        header.pop("size", None)
    line = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    return line + (data or b"")


async def read_frame(reader):
    """(nagłówek, dane albo None); None, gdy połączenie zamknięte."""
    line = await reader.readline()
    if not line:
        return None
    if len(line) > MAX_HEADER or not line.endswith(b"\n"):
        raise ProtocolError("za długi nagłówek")
    try:
        header = json.loads(line)
    except ValueError as e:
        raise ProtocolError(f"błędny JSON: {e}") from None
    if not isinstance(header, dict) or "type" not in header:
        raise ProtocolError("brak pola type")
    size = header.pop("size", 0) or 0
    if not isinstance(size, int) or size < 0 or size > MAX_DATA:
        raise ProtocolError(f"błędny rozmiar danych: {size}")
    data = await reader.readexactly(size) if size else None
    return header, data
