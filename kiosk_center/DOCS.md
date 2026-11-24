# Kiosk Center

Centrum tabletów kiosku w mieszkaniu. Tablety łączą się z nim bezpośrednio (TCP/UDP),
bez tokenu Home Assistant.

## Okno „Tablety” (pasek boczny HA)

- **Wiadomość** - duży przycisk „Nagraj wiadomość” (dotknij, mów, dotknij - do 60 s),
  pole na komunikat tekstowy, skróty (np. „Obiad gotowy”), wybór odbiorcy: wszystkie
  tablety albo jeden. Nagranie idzie w formacie przeglądarki lub telefonu, a centrum
  zamienia je na format każdego tabletu. Komunikat tekstowy centrum zamienia na mowę.
  Nagrywanie mikrofonem działa tylko przez HTTPS (Nabu Casa, aplikacja HA na telefonie).
- **Lista tabletów** - dostępność, bateria, ładowanie, moc i energia ładowania,
  przycisk „Wyślij wiadomość tylko do niego”.
- **Konfiguracja** - dodawanie, edycja i usuwanie tabletów: nazwa, adres IP, miejsce,
  model, odczytywanie komunikatów (tak/nie), format dźwięku tabletu, opcjonalnie moc
  i energia ładowarki oraz włącznik zasilania z progami ładowania. Ustawienia ogólne:
  warunek odczytu komunikatów (encja i stan, np. `switch.main_power_switch` = `on`),
  silnik mowy, skróty komunikatów, encje ekranów tabletów.

Tablet, który połączy się pierwszy raz, sam dopisuje się do konfiguracji.

## Encje (MQTT, broker Mosquitto)

Każdy tablet to urządzenie w HA: połączenie, bateria, ładowanie, temperatura baterii,
wersja aplikacji, adres IP, moc i energia ładowania (kWh - do panelu Energia) oraz
„Komunikat” - akcja `notify.send_message` czyta tekst na tym tablecie. Urządzenie
„Kiosk Center”: liczba tabletów online i „Komunikat na wszystkie tablety”.

Przykład w automatyzacji:

```yaml
action: notify.send_message
target:
  entity_id: notify.kiosk_center_komunikat_na_wszystkie_tablety
data:
  message: Pranie skończone
```

## Zasady

- Wysyłka do wszystkich trafia tylko do tabletów z włączonym odczytywaniem.
- Warunek odczytu (np. ktoś jest w domu) dotyczy każdej wysyłki; w oknie można go
  pominąć polem „Wyślij mimo warunku”.
- Sterowanie ładowaniem: włącznik zasilania tabletu włączany przy baterii poniżej progu,
  wyłączany powyżej progu, włączany też po 15 minutach bez połączenia z tabletem.

## Porty

- TCP 47700 - połączenia tabletów, UDP 47701 - odnajdywanie centrum (zmiana w opcjach).
- Protokół: `center/protocol.py`.
