# Niania

Lokalna niania elektroniczna na kamerkę USB (OBSBOT Meet SE). Działa w sieci domowej:

- **podgląd na żywo** z kamerki i **dźwięk na żywo** w przeglądarce telefonu,
- **wykrywanie płaczu** modelem YAMNet (klasy „Baby cry, infant cry” i „Crying, sobbing”), więc telewizor czy rozmowa nie włączają alarmu,
- **wykrywanie ruchu** z ignorowaniem zapalenia lub zgaszenia światła,
- **powiadomienia na telefon** przez ntfy.sh, także przy zgaszonym ekranie. Do internetu trafia tylko tekst alarmu, a obraz i dźwięk zostają w domu,
- historia zdarzeń ze zdjęciem z chwili alarmu (7 dni, `data/`),
- alarm „kamera/mikrofon nie działa”, gdy urządzenie przestanie dawać dane,
- wybór kamerki i mikrofonu w ustawieniach. Gdy wybrane urządzenie zostanie podłączone później, niania sama się na nie przełączy.

## Uruchomienie

Kliknij dwukrotnie `start.bat`, a potem otwórz na telefonie adres wypisany w oknie (np. `http://192.168.18.17:8080`).

Powiadomienia: zainstaluj aplikację **ntfy**, dodaj subskrypcję z tematem z *Ustawień* na stronie (serwer `https://ntfy.sh`) i kliknij „Wyślij test”.

## Konfiguracja

`config.json` tworzy się przy pierwszym starcie. Większość opcji ustawisz na stronie. W pliku są dodatkowo `port`, `baby_name`, `width`/`height`/`fps`, czasy wyciszenia między alarmami (`motion_cooldown_s`, `cry_cooldown_s`) i `ntfy_topic`. Temat działa jak hasło.

## Ograniczenia

- Kamerka obsługuje jedną aplikację naraz, więc gdy niania działa, nie użyjesz jej na Meet/Teams.
- Komputer nie może się usypiać (Ustawienia → System → Zasilanie → Uśpienie: Nigdy).
- Jeśli wyłączy się cały komputer, alarm „nie działa” nie dotrze. Niania jest dodatkiem, nie jedynym zabezpieczeniem.
- Strona jest dostępna dla każdego w sieci domowej (bez hasła).

## Instalacja od zera

```
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Model: `models/yamnet.tflite` z `https://storage.googleapis.com/mediapipe-models/audio_classifier/yamnet/float32/1/yamnet.tflite`.
