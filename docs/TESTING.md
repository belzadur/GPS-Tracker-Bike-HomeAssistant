# Testen

Zwei Stufen: erst am PC ohne Home Assistant, dann auf dem HA-Server ohne
Fahrrad, dann mit Fahrrad.

---

## Stufe 1 – am PC, ohne Home Assistant

Die Kernlogik (`geo.py`, `tracker.py`, `classify.py`, `storage.py`, `gpx.py`,
`elevation.py`, `segments.py`, `importer.py`) importiert bewusst kein Home
Assistant und läuft gegen synthetische GPS-Tracks.

```bash
cd gps-tracker-homeassistant
pip install -r requirements-dev.txt
python -m pytest
```

Erwartet: **83 passed**. Wenn hier etwas rot ist, brauchst du gar nicht erst auf
den HA-Server zu kopieren.

```bash
ruff check custom_components tests
```

Was abgedeckt ist:

| Datei | prüft |
|---|---|
| `test_geo.py` | Distanz, Glättung, Höhenmeter-Entrauschung, RDP |
| `test_tracker.py` | Statemachine: Start, Stopp, Stillstand-Trimmen, GPS-Sprünge |
| `test_classify.py` | Fuß/Rad/Auto anhand der Geschwindigkeitsverteilung |
| `test_storage.py` | SQLite-Schreiben/Lesen, Aggregate, Purge |
| `test_stats.py` | Zeitraumgrenzen heute/Woche/Monat/Jahr |
| `test_elevation.py` | DEM: Sampling, Batching, Cache, Interpolation, kaputte Antworten |
| `test_gpx.py` | Export→Import-Roundtrip, Namespaces, Duplikaterkennung |
| `test_segments.py` | Segment-Treffer, Umweg-Ablehnung, schnellste Runde |
| `test_merge.py` | Fahrten zusammenführen: Strecke, Pause, Notizen, unbekannte Fahrt |

---

## Stufe 2 – auf dem HA-Server, ohne Fahrrad

### 2.1 Testgerät anlegen

`scripts/replay_track.py` schiebt einen synthetischen Track über die REST-API in
einen `device_tracker`. Damit durchläuft die komplette Erkennung – Fahrtstart,
Klassifikation, Statistik, Karte – ohne dass du die Wohnung verlässt.

Erst einen Long-Lived Access Token anlegen: **Profil → ganz unten →
Langlebige Zugriffstokens → Token erstellen**.

```bash
export BIKE_TRACKER_TOKEN=eyJ...
python scripts/replay_track.py --url http://homeassistant.local:9080 --dry-run
```

`--dry-run` schickt nichts, zeigt nur die Fixes. Sieht das gut aus, ohne
`--dry-run` laufen lassen:

```bash
python scripts/replay_track.py --url http://homeassistant.local:9080
```

Das legt `device_tracker.bike_test` an und fährt eine 4-Minuten-Runde mit
22 km/h und 60 Höhenmetern. **Die Zeit läuft echt** – der Tracker stempelt jeden
Fix mit `last_updated`, ein gestauchter Replay würde als GPS-Sprung verworfen.

> Beim allerersten Mal: Skript kurz starten, abbrechen, dann die Integration
> hinzufügen und `device_tracker.bike_test` auswählen. Danach das Skript
> komplett durchlaufen lassen.

### 2.1a Der zuverlässige Kurztest

Die automatische Erkennung hat bewusst Hürden: 45 s über 7 km/h zum Starten,
danach mindestens 400 m und 2 Minuten, sonst wird die Fahrt verworfen. Zum
Prüfen der Kette stören die nur. Eine **manuell gestartete** Fahrt wird
dagegen nie wegen Kürze verworfen:

1. *Entwicklerwerkzeuge → Aktionen* → `bike_tracker.start_trip` ausführen
2. Replay laufen lassen (oder mit dem Handy einmal um den Block)
3. `bike_tracker.stop_trip` ausführen

Danach muss die Fahrt in der Liste stehen – unabhängig von allen Schwellwerten.
Klappt das, funktioniert die Kette; klappt danach die *automatische* Erkennung
nicht, liegt es an den Schwellwerten oder an zu seltenen Positionsmeldungen.

### 2.2 Was passieren muss

Während des Replays, unter **Entwicklerwerkzeuge → Zustände**:

| nach | Entität | Wert |
|---|---|---|
| ~45 s | `binary_sensor.bike_tracker_zeichnet_auf` | `on` |
| sofort | `sensor.bike_tracker_aktuelle_geschwindigkeit` | ~22 km/h |
| laufend | `sensor.bike_tracker_distanz_aktuelle_fahrt` | steigt |

Nach dem Replay entweder 150 s warten (Stoppgeschwindigkeit unterschritten) oder
`bike_tracker.stop_trip` aufrufen. Dann:

* `sensor.bike_tracker_distanz_letzte_fahrt` ≈ **1,4 km**
* `sensor.bike_tracker_hoehenmeter_letzte_fahrt` ≈ **60 hm**
* Aktivität = **Rad** (22 km/h liegt zwischen Fuß- und Auto-Grenze)
* Die Karte zeigt eine gerade Linie nach Osten plus Höhenprofil

### 2.3 Einzelne Features prüfen

**GPX-Export → Import (Roundtrip auf dem echten Server):**

```yaml
# Entwicklerwerkzeuge → Aktionen
action: bike_tracker.export_gpx
data:
  trip_id: 1
```

Datei landet in `/config/www/bike_tracker/trip_1.gpx`. Diese Fahrt löschen, dann:

```yaml
action: bike_tracker.import_gpx
data:
  path: /config/www/bike_tracker/trip_1.gpx
```

Die Antwort listet `imported` mit Distanz und Höhenmetern. Nochmal ausführen →
die Fahrt landet in `skipped` mit `already imported`.

**DEM-Höhe:** Integration → Konfigurieren → *Höhenquelle* auf **Kartendaten**.
Dann:

```yaml
action: bike_tracker.refresh_elevation
data:
  trip_id: 1
```

Die Antwort enthält die neu berechneten Höhenmeter. Der öffentliche
OpenTopoData-Dienst erlaubt 1000 Aufrufe/Tag – für den kompletten Altbestand
lieber einen eigenen Container hosten und die URL in den Optionen eintragen.
Ist der Dienst nicht erreichbar, bleibt die GPS-Höhe stehen und im Log steht
eine Warnung; es geht nichts kaputt.

**Segmente:**

```yaml
action: bike_tracker.create_segment
data:
  name: Teststrecke
  trip_id: 1
  start_index: 10
  end_index: 40
```

Die Antwort enthält `efforts_found`. Danach dasselbe Replay nochmal fahren – in
der Karte steht das Segment mit Bestzeit, und es feuert
`bike_tracker_segment_record`.

**Routing:** In der Karte auf **🗺 Route planen**, Start `home`, Ziel z. B.
`49.02,12.15`, Profil *Tour*. Ohne eigenen BRouter läuft es über den
öffentlichen OSRM-Server – der routet nach **Auto**-Profil, die Distanz stimmt
also grob, die Streckenführung ist nicht radtauglich. Für echte Radrouten einen
BRouter-Container hosten und die URL in den Optionen eintragen; sobald
„brouter" im URL steht, schaltet die Integration automatisch auf die
Radprofile um.

### 2.4 Log

```yaml
logger:
  default: warning
  logs:
    custom_components.bike_tracker: debug
```

Da steht jede erkannte und jede verworfene Fahrt mitsamt Grund drin
(`too_short:stopped`, `filtered:car`, `stale`).

---

## Stufe 3 – mit Fahrrad

Eine kurze Runde reicht, aber sie muss die Mindestwerte reißen: **mehr als
400 m und mehr als 2 Minuten**, sonst wird sie absichtlich verworfen.

Vorher in der Companion App prüfen (siehe [INSTALL.md](INSTALL.md#3-companion-app-vorbereiten)):
Hintergrund-Standort an, **High Accuracy Mode** an, Akku-Optimierung für die
App aus. Ohne High Accuracy kommt nur alle paar Minuten eine Position – dann
sind Distanz und Höhenmeter deutlich zu niedrig, und das ist mit Abstand die
häufigste Ursache für „die Zahlen stimmen nicht".

Danach `sensor.bike_tracker_distanz_letzte_fahrt` mit dem vergleichen, was
Handy-Karte oder Komoot für dieselbe Strecke sagen. Weicht es ab, hilft die
Tuning-Tabelle in [ARCHITECTURE.md](ARCHITECTURE.md#tuning).

---

## Troubleshooting

| Symptom | Ursache / Fix |
|---|---|
| Integration taucht nicht in der Liste auf | Ordner liegt nicht unter `<config>/custom_components/bike_tracker/`, oder HA wurde nicht neu gestartet. Log auf `Unable to import component` prüfen |
| Config Flow sagt „keine Attribute latitude/longitude" | Falsche Entität gewählt. `device_tracker.*` mit `source_type: gps` nehmen, nicht die Router-/Ping-Tracker |
| Karte bleibt leer / „Custom element doesn't exist" | Ressource `/local/bike-tracker-card.js` nicht eingetragen, oder Browser-Cache. Strg+F5 |
| Im Inkognito-Fenster geht es, im normalen Browser nicht | Eindeutig Cache: das normale Fenster hält die alte Datei fest. Ressourcen-URL auf `/local/bike-tracker-card.js?v=<version>` ändern – eine neue URL kann der Cache nicht überspringen |
| Zahlen ändern sich nach einem Update nicht | Nur die Karte aktualisiert, nicht die Integration. `scripts/update_on_ha.sh` holt beides, danach HA neu starten |
| Karte zeigt Kacheln nicht | Kein Internet auf dem Client, oder `tile_url` zeigt auf einen nicht erreichbaren Tile-Server |
| Kacheln zeigen „Access blocked – Referer is required" | Alte Karte, die noch die OSM-Server nutzt. Ab 0.2.2 kommen die Kacheln von CARTO – Karte per `wget` aktualisieren und Strg+F5. In der Konsole muss `BIKE-TRACKER-CARD 0.2.2` stehen |
| Kacheln zeigen „API KEY REQUIRED" | Karte bis 0.2.6 nutzt noch CARTO. Neuere Karte kopieren und Strg+F5, oder `tile_url: https://tile.openstreetmap.de/{z}/{x}/{y}.png` setzen |
| Keine Fahrten werden erkannt | High Accuracy Mode aus; oder Fahrten sind kürzer als `min_trip_distance_m` / `min_trip_duration_s`; oder `max_accuracy_m` zu streng (im Debug-Log auftauchende verworfene Punkte prüfen) |
| Position bewegt sich, aber **nie** eine Fahrt – auch nach langen, schnellen Touren | Meldet dein Gerät `speed: 0`, obwohl es fährt? Vor 0.2.6 hat der Tracker das geglaubt und die Geschwindigkeit nie aus den Positionen gerechnet, sodass keine einzige Fahrt entstand. Auf 0.2.6 aktualisieren. Prüfen lässt sich das in *Entwicklerwerkzeuge → Zustände* am Attribut `speed` deiner `device_tracker`-Entität |
| Eine Fahrt wird in mehrere zerlegt | `Dauer unter Stoppgeschwindigkeit` hoch, z. B. 300 s |
| Höhenmeter unplausibel hoch | GPS-Höhe rauscht. `Rausch-Schwelle Höhenmeter` auf 5–8 m, oder Höhenquelle auf **Kartendaten** stellen |
| Autofahrten landen als Rad | Grenzen `BIKE_MAX_P85` / `BIKE_MAX_PEAK` in `const.py`, oder einzelne Fahrten per `bike_tracker.set_activity` korrigieren |
| GPX-Import überspringt alles | Die Dateien haben keine `<time>`-Elemente – das sind geplante Routen, keine aufgezeichneten Fahrten |
| `Path ... is not allowed` | Der Pfad muss im Config-Verzeichnis liegen oder in `allowlist_external_dirs` stehen |
