# Bike Tracker für Home Assistant

Macht aus dem Standort-Stream der Home-Assistant-Companion-App automatisch
erkannte Fahrten, Statistiken, eine Karte und ein Höhenprofil – **komplett auf
dem Home Assistant**. Das Handy liefert nur die Position, es rechnet nichts.

![Status](https://img.shields.io/badge/HA-2024.11%2B-41BDF5) ![License](https://img.shields.io/badge/license-MIT-green)

## Was es kann

* **Automatische Fahrterkennung** – erkennt am geglätteten Geschwindigkeitsprofil,
  wann du losfährst, und beendet die Aufzeichnung, wenn du stehen bleibst.
* **Auto-Klassifikation Fuß / Rad / Auto** – nur Radfahrten landen in der
  Statistik, Spaziergänge und Autofahrten werden erkannt und (optional) verworfen.
* **Höhenmeter** – aus der GPS-Höhe, dreistufig entrauscht (Median-Filter →
  gleitender Mittelwert → Hysterese), damit nicht jede GPS-Zappelei als Anstieg zählt.
* **Statistik-Sensoren** – Distanz, Fahrzeit, Fahrten und Höhenmeter für
  heute / Woche / Monat / Jahr / gesamt, plus Live-Sensoren während der Fahrt.
* **Lovelace-Karte** – Track auf OpenStreetMap, Höhenprofil, Balkendiagramm der
  letzten 30 Tage, Fahrtenliste, Aktivität nachträglich korrigieren.
* **Höhe aus Kartendaten** – optional statt GPS-Höhe: ein Höhenmodell
  (OpenTopoData/Open-Elevation) liefert reproduzierbare Höhenmeter, dieselbe
  Runde ergibt immer dieselbe Zahl.
* **GPX-Export und -Import** – pro Fahrt exportieren, bestehende Aufzeichnungen
  einlesen (mit Duplikaterkennung).
* **Segmente & Bestzeiten** – benannte Streckenabschnitte, jede Fahrt wird
  automatisch dagegen geprüft, eine neue Bestzeit feuert ein Event.
* **Routenplanung** – OSRM- und BRouter-Backend, bedienbar direkt in der Karte,
  optional als Push an die Companion App.
* **Services & Events** für eigene Automationen.

Alle Fahrten liegen in einer eigenen SQLite-Datei (`config/bike_tracker.db`),
nicht im Recorder – sie überleben also jeden Recorder-Purge und blähen die
HA-Datenbank nicht auf.

## Installation

### HACS (empfohlen)

1. HACS → **⋮ → Benutzerdefinierte Repositories**
2. URL dieses Repos eintragen, Kategorie **Integration**
3. „Bike Tracker" herunterladen, Home Assistant neu starten

> Das ist eine **Integration**, kein Add-on. Unter *Einstellungen → Add-ons →
> Store → ⋮ → Repositories* eingetragen kommt „is not a valid add-on
> repository" – das ist die falsche Stelle.

### Manuell

```bash
# Ordner custom_components/bike_tracker in dein HA-config-Verzeichnis kopieren
scp -r custom_components/bike_tracker root@homeassistant:/config/custom_components/
```

Oder mit dem mitgelieferten Skript, das zusätzlich die Karte kopiert:

```bash
./scripts/deploy_to_ha.sh root@homeassistant.local 22 /pfad/zum/config
```

Danach Home Assistant neu starten. Details und Docker-Pfade: [docs/INSTALL.md](docs/INSTALL.md).

### Einrichten

**Einstellungen → Geräte & Dienste → Integration hinzufügen → „Bike Tracker"**,
dann den `device_tracker` deines Handys auswählen (z. B.
`device_tracker.pixel_8`).

> **Wichtig:** In der Companion App unter *Einstellungen → Companion App →
> Sensoren verwalten → Hintergrund-Standort* den **High Accuracy Mode**
> aktivieren. Ohne ihn kommt nur alle paar Minuten eine Position – dann sind
> Distanz und Höhenmeter deutlich zu niedrig.

## Die Lovelace-Karte

```bash
cp www/bike-tracker-card.js /config/www/
```

Dann **Einstellungen → Dashboards → ⋮ → Ressourcen → Ressource hinzufügen**:
`/local/bike-tracker-card.js`, Typ *JavaScript-Modul*.

```yaml
type: custom:bike-tracker-card
title: Fahrradstatistik
activity: bike        # bike | walk | car | leer für alle
limit: 25             # Fahrten in der Liste
days: 30              # Zeitraum des Balkendiagramms
default_period: week  # today | week | month | year | total
map_height: 420       # Höhe der Karte in Pixeln
show_route: true      # Panel „Route planen"
show_segments: true   # Segmentliste mit Bestzeiten
# tile_url: http://dein-tileserver:8080/tile/{z}/{x}/{y}.png
#   ohne Angabe kommen die Kacheln von OpenStreetMap Deutschland
# tile_attribution: "© OpenStreetMap-Mitwirkende"   # Quellenhinweis zum eigenen tile_url
# notify_service: notify.mobile_app_pixel_9   # nötig für „ans Handy" im Routen-Panel
```

Weitere Beispiele: [lovelace-example.yaml](lovelace-example.yaml).

### Kartenkacheln

Ohne `tile_url` kommen die Kacheln von **OpenStreetMap Deutschland**
(`tile.openstreetmap.de`). Der Dienst ist für private Nutzung ohne
Registrierung freigegeben ([Nutzungsbedingungen](https://openstreetmap.de/nutzen/dienste-osm-de/)).
Eine dunkle Variante gibt es dort nicht.

Warum nicht CARTO (bis 0.2.6): CARTO liefert ohne API-Schlüssel nur noch eine
*„API KEY REQUIRED"*-Grafik. Warum nicht `tile.openstreetmap.org`: dessen
ehrenamtlich betriebene Server lehnen Anfragen aus einem LAN-Zugriff auch mit
gesetztem Referer ab (*„Access blocked"*).

Eigener Tile-Server, falls vorhanden – `tile_attribution` setzt den passenden
Quellenhinweis unten rechts in der Karte:

```yaml
tile_url: http://dein-tileserver:8080/tile/{z}/{x}/{y}.png
tile_attribution: "© OpenStreetMap-Mitwirkende"
```

## Entitäten

| Entität | Beschreibung |
|---|---|
| `binary_sensor.bike_tracker_zeichnet_auf` | An, solange eine Fahrt aufgezeichnet wird |
| `sensor.bike_tracker_aktuelle_geschwindigkeit` | Geglättete Live-Geschwindigkeit |
| `sensor.bike_tracker_distanz_aktuelle_fahrt` | Live-Distanz, Attribut `trip` enthält den Track |
| `sensor.bike_tracker_distanz_letzte_fahrt` | Letzte Radfahrt, viele Attribute |
| `sensor.bike_tracker_distanz_{heute,woche,monat,jahr,gesamt}` | Distanz je Zeitraum |
| `sensor.bike_tracker_fahrzeit_…` / `fahrten_…` / `hoehenmeter_…` | dito |

## Services

| Service | Zweck |
|---|---|
| `bike_tracker.start_trip` / `stop_trip` / `discard_trip` | Aufzeichnung manuell steuern |
| `bike_tracker.set_activity` | Erkannte Aktivität einer Fahrt korrigieren |
| `bike_tracker.delete_trip` | Fahrt löschen |
| `bike_tracker.export_gpx` | Fahrt als GPX schreiben |
| `bike_tracker.import_gpx` | GPX-Datei oder ganzen Ordner als Fahrten einlesen |
| `bike_tracker.refresh_elevation` | Höhenmeter bestehender Fahrten aus dem Höhenmodell neu rechnen |
| `bike_tracker.create_segment` / `delete_segment` / `rescan_segments` | Segmente verwalten |
| `bike_tracker.plan_route` | Route planen, optional an die App pushen |
| `bike_tracker.purge` | Alte Fahrten löschen |

## Events

`bike_tracker_trip_started`, `bike_tracker_trip_finished`,
`bike_tracker_trip_discarded`, `bike_tracker_route_planned`,
`bike_tracker_segment_matched`, `bike_tracker_segment_record`.

```yaml
automation:
  - alias: Fahrt-Zusammenfassung
    trigger:
      - platform: event
        event_type: bike_tracker_trip_finished
    condition: "{{ trigger.event.data.activity == 'bike' }}"
    action:
      - service: notify.mobile_app_pixel_8
        data:
          title: "Fahrt aufgezeichnet"
          message: >-
            {{ trigger.event.data.distance_km }} km in
            {{ trigger.event.data.duration_min }} min,
            Ø {{ trigger.event.data.avg_speed_kmh }} km/h,
            {{ trigger.event.data.elevation_gain_m }} hm
```

## REST-API

Alle Endpunkte brauchen ein gültiges HA-Token.

| Endpunkt | |
|---|---|
| `GET /api/bike_tracker/trips?limit=50&activity=bike&days=30` | Fahrtenliste |
| `GET /api/bike_tracker/trips/{id}` | Fahrt inkl. Track, Höhen- und Tempoprofil |
| `POST /api/bike_tracker/trips/{id}` | `{"activity": "...", "note": "..."}` |
| `DELETE /api/bike_tracker/trips/{id}` | löschen |
| `GET /api/bike_tracker/trips/{id}/track?tolerance=5` | vereinfachter Track |
| `GET /api/bike_tracker/trips/{id}/gpx` | GPX-Download |
| `GET /api/bike_tracker/stats?days=30` | Aggregate + Tageswerte |
| `GET /api/bike_tracker/current` | laufende Fahrt |
| `GET /api/bike_tracker/segments` | Segmente inkl. Bestzeit |
| `GET`/`DELETE` `/api/bike_tracker/segments/{id}` | Segment inkl. aller Zeiten |
| `POST /api/bike_tracker/route` | `{"start": "...", "destination": "...", "profile": "bike"}` |

## Einstellungen justieren

**Integration → Konfigurieren.** Die Standardwerte passen für die Companion App
im High Accuracy Mode. Wenn Fahrten zu spät starten oder zu früh enden, sind
`Startgeschwindigkeit`, `Dauer über Startgeschwindigkeit` und
`Dauer unter Stoppgeschwindigkeit` die richtigen Stellschrauben – siehe
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#tuning).

## Entwicklung

```bash
pip install -r requirements-dev.txt
pytest      # 83 Tests, laufen ohne Home-Assistant-Installation
ruff check custom_components tests
```

Die Kernlogik (`geo.py`, `tracker.py`, `classify.py`, `storage.py`, `gpx.py`,
`elevation.py`, `segments.py`, `importer.py`) importiert bewusst kein Home
Assistant und wird gegen synthetische GPS-Tracks getestet.

Wie du die Integration auf dem Server durchtestest – inklusive eines Skripts,
das eine Fahrt simuliert, damit du dafür nicht rausfahren musst – steht in
[docs/TESTING.md](docs/TESTING.md).

## Lizenz

MIT
