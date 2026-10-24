# Changelog

## Unveröffentlicht

* **Fahrten zusammenführen:** `POST /api/bike_tracker/merge` mit
  `{"trip_ids": [4, 5]}` legt mehrere Fahrten zu einer zusammen – etwa wenn
  eine Pause an der Ampel oder beim Bäcker die Fahrt zerteilt hat. Die Punkte
  aller Fahrten werden nach Zeit sortiert und mit `trip_from_points` neu
  gerechnet (Strecke, Dauer, Fahrzeit, Tempo, Höhe, Aktivität), Notizen
  aneinandergehängt, die Ausgangsfahrten gelöscht, Statistik und Segmente neu
  geprüft. Die Pause zwischen den Fahrten zählt zur Dauer, nicht zur Fahrzeit.
  Haben alle Fahrten dieselbe Aktivität, bleibt sie erhalten, sonst wird neu
  klassifiziert. Logik in `merge.py`, ohne Home Assistant testbar.
* Drei Tests dazu (`test_merge.py`).

## 0.2.6

* **Fahrten wurden nie erkannt, wenn das Handy keine Geschwindigkeit liefert.**
  Der Tracker bevorzugte die vom Gerät gemeldete Geschwindigkeit, akzeptierte
  dabei aber auch eine gemeldete **0**. Androids Companion-App füllt unbekannte
  Felder mit 0, statt sie wegzulassen – ein Gerät ohne Speed-Fix meldet also
  `speed: 0`, während sich seine Position quer durch die Stadt bewegt. Damit
  blieb die geglättete Geschwindigkeit dauerhaft bei 0, die Startschwelle von
  7 km/h wurde nie überschritten, und es entstand **keine einzige Fahrt** –
  egal wie weit und schnell gefahren wurde. Eine gemeldete 0 gilt jetzt als
  „keine Angabe", und die Geschwindigkeit wird aus den Positionen gerechnet.
  Bei echtem Stillstand ändert das nichts, weil die gerechnete Geschwindigkeit
  dann ebenfalls bei null liegt.
* Drei Regressionstests dazu: Fahrt trotz gemeldeter 0 erkannt, echter
  Stillstand bleibt Stillstand, echte Geräteangabe gewinnt weiterhin.

## 0.2.5

* Kartenhöhe auf **500 px**, weiterhin über `map_height` einstellbar.
* Die **Version der Karte steht jetzt in der Attribution** unten rechts auf der
  Karte (`Karte v0.2.5`). Damit lässt sich ohne Browser-Konsole prüfen, welcher
  Stand tatsächlich geladen ist – nach einem Update die schnellste Probe, ob
  der Cache noch die alte Datei festhält.

## 0.2.4

* **`scripts/update_on_ha.sh`** – aktualisiert Integration **und** Karte in
  einem Rutsch direkt aus dem Repo. Die Karte allein per `wget` zu holen
  reichte nie: Korrekturen an Statistik, Höhenberechnung und Services stecken
  in `custom_components/`, nicht in der `.js`-Datei. Wer nur die Karte
  aktualisierte, wunderte sich anschließend über unveränderte Zahlen.
* Kartenhöhe von 280 auf **420 px** erhöht und über `map_height`
  konfigurierbar gemacht.

## 0.2.3

* **Statistik-Kacheln zeigten veraltete Zahlen.** Der Endpunkt `/stats` las die
  Zeiträume aus einem Cache im Arbeitsspeicher, das Balkendiagramm dagegen live
  aus der Datenbank – nebeneinander in derselben Karte. Der Cache wurde nur
  aktualisiert, wenn die Integration selbst eine Fahrt speicherte; Fahrten aus
  einem GPX-Import, dem Demo-Seeder oder einer direkten Datenbankänderung
  blieben unsichtbar, gelöschte weiterhin sichtbar. Die Zeiträume kommen jetzt
  ebenfalls direkt aus der Datenbank.
* Die **Sensoren** rechnen ihre Aggregate zusätzlich alle 5 Minuten neu, damit
  auch sie externe Änderungen mitbekommen – während einer laufenden Fahrt
  ausgesetzt.
* `seed_demo_data.py --status` zeigt, was tatsächlich in der Datenbank steht:
  Anzahl Fahrten, davon Demofahrten, neueste Fahrt und alle fünf Zeiträume.
* Der Seeder legt jetzt garantiert eine Fahrt **heute** und eine **gestern** an.
  Vorher rechnete er in ganzen Wochen zurück, sodass an einem Montag oder
  Dienstag „Heute" und „Woche" leer blieben und wie ein Defekt aussahen.

## 0.2.2

* **Kachel-Quelle gewechselt: CARTO statt OpenStreetMap.** Der Referrer-Fix aus
  0.2.1 reichte nicht – aus einem LAN-Zugriff heraus liefert OSM weiterhin die
  Sperr-Grafik. Nachgewiesen mit einem Gegentest über Stadt und Ozean: OSM gab
  für beide Orte ein identisches Bild zurück (55 Farben, Helligkeit 228),
  CARTO dagegen inhaltsabhängige Kacheln. CARTO liefert auch ohne Referer.
* Die Kacheln folgen jetzt dem **HA-Theme**: `light_all` im hellen, `dark_all`
  im dunklen Design, Umschaltung ohne Neuladen. `tile_url` überschreibt das
  weiterhin für einen eigenen Tile-Server.

## 0.2.1

* **„Access blocked" auf den Kacheln behoben** – OpenStreetMaps Kachel-Server
  verlangen einen `Referer`, Home Assistant liefert sein Frontend aber mit
  strenger `Referrer-Policy` aus. Die Karte setzt die Policy jetzt direkt am
  Kachel-Bild, was die Dokument-Policy überschreibt. Nachgemessen: ohne den
  Fix liefert OSM 6.987 Bytes (die Sperr-Grafik), mit Fix 23.295 Bytes (eine
  echte Kachel) – beide mit HTTP 200, weshalb es leicht zu übersehen ist.
* **Karte repariert** – die OSM-Kacheln lagen versprengt herum statt eine
  zusammenhängende Karte zu ergeben. Drei Ursachen: `leaflet.css` landete nur
  in `document.head` und erreichte den Shadow Root der Karte nie, Leaflet wurde
  bei jedem Rendern komplett neu aufgebaut, und `fitBounds` lief gegen eine
  veraltete Containergröße.
* Die Karte erscheint jetzt auch **ohne Fahrten**, zentriert auf den
  Home-Assistant-Standort, statt als graues Rechteck
* Das Routen-Panel erklärt, dass es Koordinaten oder Zonennamen erwartet – eine
  Ortssuche gibt es nicht
* `scripts/seed_demo_data.py` – füllt die Datenbank mit einer Saison
  Demofahrten, damit sich Diagramme, Höhenprofil, Karte und Segmente ohne
  wochenlanges Sammeln prüfen lassen; rückstandsfrei entfernbar mit `--remove`
* `scripts/deploy_to_ha.sh` – kopiert Integration und Karte per scp

## 0.2.0

Arbeitet die vier offenen Punkte aus 0.1.0 ab.

### Höhe aus Kartendaten

* Neue Höhenquelle **dem**: fragt ein Höhenmodell ab (OpenTopoData oder
  Open-Elevation, Backend wird an der URL erkannt) statt die GPS-Höhe zu nutzen
* Sampling alle ~25 m, Batches à 100 Koordinaten, 1 Request/s, Cache über
  gerundete Koordinaten – dieselbe Straße kostet beim zweiten Mal nichts
* Neue Option **URL des Höhendienstes**
* Neuer Service `refresh_elevation`, rechnet bestehende Fahrten nach
* Fällt der Dienst aus, bleibt die GPS-Höhe stehen – eine Fahrt geht nie verloren

### GPX-Import

* Neuer Service `import_gpx` für eine Datei oder einen ganzen Ordner
* Namespace-toleranter Parser (GPX 1.0 und 1.1), mehrere `<trk>` und `<trkseg>`
* Dateien ohne Zeitstempel (geplante Routen) werden mit klarer Begründung
  abgelehnt statt als Null-Sekunden-Fahrt gespeichert
* Duplikaterkennung über Zeitüberlappung
* `trip_from_points()` rechnet importierte Fahrten mit derselben Logik wie
  live aufgezeichnete

### Segmente & Bestzeiten

* Neue Tabellen `segments` und `segment_efforts` (Schema-Version 2, migriert
  beim Start von selbst)
* `match_segment()` mit Proximity-Erkennung und Längen-Plausibilitätsprüfung,
  von mehreren Runden gewinnt die schnellste
* Services `create_segment`, `delete_segment`, `rescan_segments`
* Events `segment_matched` und `segment_record` (neue Bestzeit)
* REST: `GET /segments`, `GET`/`DELETE /segments/{id}`

### Routing-UI

* Panel „Route planen" in der Lovelace-Karte: Start/Ziel (Koordinaten oder
  Zonennamen), Profil, optionaler Push ans Handy
* Route wird gestrichelt über den Track gelegt
* Neuer Endpunkt `POST /api/bike_tracker/route`
* Neue Karten-Optionen `show_route`, `show_segments`, `notify_service`

### Sonstiges

* `scripts/replay_track.py` simuliert eine Fahrt über die REST-API – die
  komplette Erkennung lässt sich damit ohne Fahrrad testen
* Neue Doku `docs/TESTING.md`
* 83 statt 41 Tests, weiterhin ohne Home-Assistant-Installation lauffähig

## 0.1.0 – erste Version

* Automatische Fahrterkennung aus dem `device_tracker` der Companion App
  (Statemachine mit Kandidaten-Puffer, Stillstands-Trimmen, Sprung- und
  Genauigkeitsfilter)
* Klassifikation Fuß / Rad / Auto anhand der Geschwindigkeitsverteilung
* Höhenmeter aus GPS-Höhe, dreistufig entrauscht
* SQLite-Persistenz in `config/bike_tracker.db` inkl. Trackpunkten
* 28 Statistik-Sensoren (heute/Woche/Monat/Jahr/gesamt) plus Live-Sensoren
* Config Flow und Options Flow, Übersetzungen DE/EN
* Services: `start_trip`, `stop_trip`, `discard_trip`, `set_activity`,
  `delete_trip`, `export_gpx`, `plan_route`, `purge`
* Events: `trip_started`, `trip_finished`, `trip_discarded`, `route_planned`
* REST-API für Fahrten, Tracks, Statistik, GPX und laufende Fahrt
* Lovelace-Karte mit Leaflet-Track, Höhenprofil, Balkendiagramm und
  Aktivitäts-Korrektur
* GPX-1.1-Export
* Routing-Grundgerüst mit OSRM- und BRouter-Backend
* 41 Unit-Tests gegen synthetische GPS-Tracks, ohne HA-Installation lauffähig
