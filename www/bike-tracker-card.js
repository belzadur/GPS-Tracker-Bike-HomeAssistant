/*
 * Bike Tracker card for Home Assistant
 * https://github.com/skriptedd/GPS-Tracker-Bike-HomeAssistant
 *
 * Single file, no build step. Leaflet is pulled from a CDN on first use;
 * set `leaflet_url` / `tile_url` in the card config to point at local copies
 * if your instance has no internet access.
 */

const LEAFLET_JS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js";
const LEAFLET_CSS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css";
// OpenStreetMap Deutschland statt CARTO: CARTO liefert ohne API-Schlüssel nur
// noch eine "API KEY REQUIRED"-Grafik. Der Server von OSM Deutschland ist für
// private Nutzung ohne Registrierung freigegeben
// (https://openstreetmap.de/nutzen/dienste-osm-de/) und nimmt die Anfragen aus
// dem HA-Frontend an, weil die Kacheln über referrerPolicy den Origin
// mitschicken. Eine dunkle Variante gibt es dort nicht.
const DEFAULT_TILE_URL = "https://tile.openstreetmap.de/{z}/{x}/{y}.png";
const CARD_VERSION = "0.2.6";
// Die Version steht mit in der Attribution: so lässt sich auf einen Blick
// sehen, welcher Stand geladen ist, ohne die Browser-Konsole zu öffnen.
// Nach einem Update ist das die schnellste Probe, ob der Cache noch die alte
// Datei festhält.
const TILE_ATTRIBUTION =
  '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap-Mitwirkende</a>, ' +
  '<a href="https://tile.openstreetmap.de/">OSM Deutschland</a>';

const ACTIVITY_ICON = { bike: "🚲", walk: "🚶", car: "🚗", unknown: "❓" };
const ACTIVITY_LABEL = { bike: "Rad", walk: "Fuß", car: "Auto", unknown: "?" };

let leafletPromise = null;
function loadLeaflet(jsUrl, cssUrl) {
  if (window.L) return Promise.resolve(window.L);
  if (leafletPromise) return leafletPromise;
  leafletPromise = new Promise((resolve, reject) => {
    // Leaflet appends popups and controls to the document, so it needs its
    // stylesheet here too. The copy inside the shadow root (see _render) is
    // what actually positions the tiles - styles do not cross that boundary.
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = cssUrl || LEAFLET_CSS;
    document.head.appendChild(link);
    const script = document.createElement("script");
    script.src = jsUrl || LEAFLET_JS;
    script.onload = () => resolve(window.L);
    script.onerror = () => reject(new Error("Leaflet konnte nicht geladen werden"));
    document.head.appendChild(script);
  });
  return leafletPromise;
}

const esc = (value) =>
  String(value == null ? "" : value).replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
  );

const fmtKm = (m) => (m == null ? "–" : (m / 1000).toFixed(1).replace(".", ","));
const fmtNum = (v, d = 1) =>
  v == null ? "–" : Number(v).toFixed(d).replace(".", ",");

function fmtDuration(seconds) {
  if (!seconds) return "0 min";
  const total = Math.round(seconds / 60);
  const h = Math.floor(total / 60);
  const m = total % 60;
  return h ? `${h} h ${String(m).padStart(2, "0")} min` : `${m} min`;
}

function fmtDate(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleDateString("de-DE", {
    weekday: "short",
    day: "2-digit",
    month: "2-digit",
  });
}

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString("de-DE", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

class BikeTrackerCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._trips = [];
    this._stats = null;
    this._selected = null;
    this._detail = null;
    this._loaded = false;
    this._period = "week";
    this._map = null;
    this._mapHolder = null;
    this._tileLayer = null;
    this._resizeObserver = null;
    this._pendingLayout = null;
    this._layer = null;
    this._routeLayer = null;
    this._error = null;
    this._segments = [];
    this._routeOpen = false;
    this._routeBusy = false;
    this._routeError = null;
    this._routeResult = null;
    // Kept outside the DOM because _render() rebuilds innerHTML wholesale.
    this._routeForm = { start: "", destination: "", profile: "bike", notify: false };
  }

  setConfig(config) {
    this._config = {
      title: "Fahrradstatistik",
      activity: "bike",
      limit: 25,
      days: 30,
      // tile_url absichtlich ohne Vorgabe, siehe _tileUrl(); tile_attribution
      // ersetzt den Quellenhinweis, wenn ein eigener Kachel-Server eingetragen ist.
      referrer_policy: "strict-origin-when-cross-origin",
      map_height: 500,
      show_route: true,
      show_segments: true,
      ...config,
    };
    this._period = this._config.default_period || "week";
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._loaded) {
      this._loaded = true;
      this._render();
      this._refresh();
    } else {
      this._updateLive();
    }
  }

  getCardSize() {
    return 12;
  }

  // ---------------------------------------------------------------- data

  async _api(path, options) {
    return this._hass.callApi(
      (options && options.method) || "GET",
      path,
      options && options.body
    );
  }

  async _refresh() {
    try {
      this._error = null;
      const query = `bike_tracker/trips?limit=${this._config.limit}` +
        (this._config.activity ? `&activity=${this._config.activity}` : "");
      const [trips, stats, segments] = await Promise.all([
        this._api(query),
        this._api(`bike_tracker/stats?days=${this._config.days}&activity=${this._config.activity}`),
        this._config.show_segments
          ? this._api("bike_tracker/segments").catch(() => ({ segments: [] }))
          : Promise.resolve({ segments: [] }),
      ]);
      this._trips = trips.trips || [];
      this._stats = stats;
      this._segments = segments.segments || [];
      if (!this._selected && this._trips.length) {
        await this._select(this._trips[0].id);
      } else {
        this._render();
      }
    } catch (err) {
      this._error = err && err.message ? err.message : String(err);
      this._render();
    }
  }

  async _select(tripId) {
    this._selected = tripId;
    this._render();
    try {
      this._detail = await this._api(`bike_tracker/trips/${tripId}`);
    } catch (err) {
      this._detail = null;
      this._error = `Fahrt ${tripId} konnte nicht geladen werden`;
    }
    this._render();
    this._drawMap();
  }

  async _setActivity(tripId, activity) {
    await this._api(`bike_tracker/trips/${tripId}`, {
      method: "POST",
      body: { activity, confirmed: true },
    });
    await this._refresh();
  }

  async _delete(tripId) {
    if (!confirm("Diese Fahrt wirklich löschen?")) return;
    await this._api(`bike_tracker/trips/${tripId}`, { method: "DELETE" });
    this._selected = null;
    this._detail = null;
    await this._refresh();
  }

  async _deleteSegment(segmentId) {
    if (!confirm("Dieses Segment und alle Zeiten wirklich löschen?")) return;
    await this._api(`bike_tracker/segments/${segmentId}`, { method: "DELETE" });
    await this._refresh();
  }

  async _planRoute() {
    const form = this._routeForm;
    if (!form.start.trim() || !form.destination.trim()) {
      this._routeError = "Start und Ziel angeben";
      this._render();
      return;
    }
    this._routeBusy = true;
    this._routeError = null;
    this._render();

    const body = {
      start: form.start.trim(),
      destination: form.destination.trim(),
      profile: form.profile,
    };
    if (form.notify) {
      const notify = this._config.notify_service;
      if (!notify) {
        this._routeBusy = false;
        this._routeError =
          "Für den Push muss notify_service in der Kartenkonfiguration stehen";
        this._render();
        return;
      }
      body.notify_device = notify;
    }

    try {
      this._routeResult = await this._api("bike_tracker/route", {
        method: "POST",
        body,
      });
    } catch (err) {
      this._routeResult = null;
      this._routeError =
        (err && (err.body && err.body.message)) ||
        (err && err.message) ||
        "Route konnte nicht berechnet werden";
    }
    this._routeBusy = false;
    this._render();
    this._drawRoute();
  }

  _clearRoute() {
    this._routeResult = null;
    this._routeError = null;
    if (this._routeLayer) {
      this._routeLayer.remove();
      this._routeLayer = null;
    }
    this._render();
  }

  _updateLive() {
    const banner = this.shadowRoot.getElementById("live");
    if (!banner || !this._hass) return;
    const recording = Object.values(this._hass.states).find(
      (s) => s.entity_id.startsWith("binary_sensor.") && s.attributes.state_machine
    );
    if (recording && recording.state === "on") {
      banner.style.display = "flex";
      banner.querySelector(".speed").textContent =
        fmtNum(recording.attributes.speed_kmh, 1) + " km/h";
    } else if (banner) {
      banner.style.display = "none";
    }
  }

  // -------------------------------------------------------------- render

  _render() {
    if (!this.shadowRoot) return;
    const periods = this._stats ? this._stats.periods || {} : {};
    const current = periods[this._period] || {};

    this.shadowRoot.innerHTML = `
      <link rel="stylesheet" href="${this._config.leaflet_css || LEAFLET_CSS}">
      <style>${STYLES}</style>
      <ha-card>
        <div class="header">
          <div class="title">${this._config.title}</div>
          <div class="tabs">
            ${["today", "week", "month", "year", "total"]
              .map(
                (p) =>
                  `<button class="tab ${p === this._period ? "active" : ""}" data-period="${p}">${
                    { today: "Heute", week: "Woche", month: "Monat", year: "Jahr", total: "Gesamt" }[p]
                  }</button>`
              )
              .join("")}
          </div>
        </div>

        <div id="live" class="live" style="display:none">
          <span class="dot"></span><span>Aufzeichnung läuft</span>
          <span class="speed"></span>
        </div>

        ${this._error ? `<div class="error">${this._error}</div>` : ""}

        <div class="kpis">
          ${this._kpi(fmtKm(current.distance_m) + " km", "Distanz")}
          ${this._kpi(fmtDuration(current.moving_time_s), "Fahrzeit")}
          ${this._kpi(String(current.trips || 0), "Fahrten")}
          ${this._kpi(Math.round(current.elevation_gain_m || 0) + " hm", "Höhenmeter")}
          ${this._kpi(fmtNum(current.avg_speed_kmh, 1) + " km/h", "Schnitt")}
          ${this._kpi(fmtNum(current.max_speed_kmh, 1) + " km/h", "Maximum")}
        </div>

        ${this._barChart()}
        ${this._routePanel()}

        <div id="map" class="map" style="height:${
          Number(this._config.map_height) || 500
        }px"></div>
        ${this._detailPanel()}
        ${this._elevationChart()}
        ${this._segmentList()}
        ${this._tripList()}
      </ha-card>
    `;

    // The Leaflet container lives outside the markup we just replaced - put
    // it straight back, before anything can measure a zero-sized slot.
    if (this._mapHolder) {
      const slot = this.shadowRoot.getElementById("map");
      if (slot && this._mapHolder.parentNode !== slot) {
        slot.textContent = "";
        slot.appendChild(this._mapHolder);
      }
    }

    this.shadowRoot.querySelectorAll(".tab").forEach((el) =>
      el.addEventListener("click", () => {
        this._period = el.dataset.period;
        this._render();
        this._drawMap();
      })
    );
    this.shadowRoot.querySelectorAll("[data-trip]").forEach((el) =>
      el.addEventListener("click", () => this._select(Number(el.dataset.trip)))
    );
    this.shadowRoot.querySelectorAll("[data-activity]").forEach((el) =>
      el.addEventListener("click", (ev) => {
        ev.stopPropagation();
        this._setActivity(Number(el.dataset.tripId), el.dataset.activity);
      })
    );
    const del = this.shadowRoot.getElementById("delete");
    if (del) del.addEventListener("click", () => this._delete(this._selected));
    this._bindRoutePanel();
    this.shadowRoot.querySelectorAll("[data-segment-delete]").forEach((el) =>
      el.addEventListener("click", (ev) => {
        ev.stopPropagation();
        this._deleteSegment(Number(el.dataset.segmentDelete));
      })
    );
    this._updateLive();
    this._drawMap();
    this._drawRoute();
  }

  _bindRoutePanel() {
    const toggle = this.shadowRoot.getElementById("route-toggle");
    if (toggle)
      toggle.addEventListener("click", () => {
        this._routeOpen = !this._routeOpen;
        this._render();
      });

    // Write straight through to _routeForm - the next _render() wipes the DOM.
    const bind = (id, key) => {
      const el = this.shadowRoot.getElementById(id);
      if (!el) return;
      const store = () => {
        this._routeForm[key] = el.type === "checkbox" ? el.checked : el.value;
      };
      el.addEventListener("input", store);
      el.addEventListener("change", store);
    };
    bind("route-start", "start");
    bind("route-destination", "destination");
    bind("route-profile", "profile");
    bind("route-notify", "notify");

    const go = this.shadowRoot.getElementById("route-go");
    if (go) go.addEventListener("click", () => this._planRoute());
    const clear = this.shadowRoot.getElementById("route-clear");
    if (clear) clear.addEventListener("click", () => this._clearRoute());
    const here = this.shadowRoot.getElementById("route-here");
    if (here)
      here.addEventListener("click", () => {
        this._routeForm.start = "home";
        this._render();
      });
  }

  _kpi(value, label) {
    return `<div class="kpi"><div class="v">${value}</div><div class="l">${label}</div></div>`;
  }

  _detailPanel() {
    const t = this._detail;
    if (!t) {
      // Without any trips the list below already says so - do not stack two
      // empty-state messages on top of each other.
      return this._trips.length
        ? `<div class="empty">Fahrt auswählen</div>`
        : "";
    }
    const gpxUrl = `/api/bike_tracker/trips/${t.id}/gpx`;
    return `
      <div class="detail">
        <div class="detail-head">
          <div>
            <div class="dt">${ACTIVITY_ICON[t.activity] || ""} ${fmtDate(t.started_at)}
              ${fmtTime(t.started_at)}–${fmtTime(t.ended_at)}</div>
            <div class="sub">Erkannt als <b>${ACTIVITY_LABEL[t.activity] || t.activity}</b>
              (${Math.round((t.activity_confidence || 0) * 100)} % sicher)</div>
          </div>
          <div class="actions">
            ${["bike", "walk", "car"]
              .map(
                (a) =>
                  `<button class="chip ${a === t.activity ? "on" : ""}" data-activity="${a}" data-trip-id="${t.id}">${ACTIVITY_ICON[a]}</button>`
              )
              .join("")}
            <a class="chip" href="${gpxUrl}" download>GPX</a>
            <button class="chip danger" id="delete">🗑</button>
          </div>
        </div>
        <div class="grid">
          ${this._kpi(fmtKm(t.distance_m) + " km", "Strecke")}
          ${this._kpi(fmtDuration(t.moving_time_s), "in Bewegung")}
          ${this._kpi(fmtNum(t.avg_moving_kmh, 1) + " km/h", "Schnitt")}
          ${this._kpi(fmtNum(t.max_speed_kmh, 1) + " km/h", "Maximum")}
          ${this._kpi(Math.round(t.elevation_gain_m || 0) + " hm", "bergauf")}
          ${this._kpi(Math.round(t.elevation_loss_m || 0) + " hm", "bergab")}
        </div>
      </div>`;
  }

  _barChart() {
    const daily = (this._stats && this._stats.daily) || [];
    if (!daily.length) return "";
    const max = Math.max(...daily.map((d) => d.distance_m || 0), 1);
    const width = 100 / Math.max(daily.length, 1);
    const bars = daily
      .map((d, i) => {
        const h = ((d.distance_m || 0) / max) * 100;
        return `<div class="bar" style="left:${i * width}%;width:${width * 0.8}%;height:${h}%"
          title="${d.day}: ${fmtKm(d.distance_m)} km"></div>`;
      })
      .join("");
    return `<div class="chart-wrap">
      <div class="chart-title">Letzte ${this._config.days} Tage · max ${fmtKm(max)} km</div>
      <div class="bars">${bars}</div>
    </div>`;
  }

  _elevationChart() {
    const profile = (this._detail && this._detail.elevation_profile) || [];
    if (profile.length < 3) return "";
    const xs = profile.map((p) => p[0]);
    const ys = profile.map((p) => p[1]);
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);
    const spanY = Math.max(maxY - minY, 1);
    const maxX = Math.max(...xs, 0.001);
    const pts = profile
      .map((p) => `${(p[0] / maxX) * 300},${40 - ((p[1] - minY) / spanY) * 34}`)
      .join(" ");
    return `<div class="chart-wrap">
      <div class="chart-title">Höhenprofil · ${Math.round(minY)}–${Math.round(maxY)} m</div>
      <svg viewBox="0 0 300 44" preserveAspectRatio="none" class="elev">
        <polygon points="0,44 ${pts} 300,44" />
        <polyline points="${pts}" />
      </svg>
    </div>`;
  }

  _routePanel() {
    if (!this._config.show_route) return "";
    const form = this._routeForm;
    const result = this._routeResult;
    if (!this._routeOpen) {
      return `<div class="route-bar">
        <button class="chip" id="route-toggle">🗺 Route planen</button>
        ${
          result
            ? `<span class="route-sum">${fmtNum(result.distance_km, 1)} km · ${fmtDuration(
                result.duration_min * 60
              )}</span>`
            : ""
        }
      </div>`;
    }

    const profiles = [
      ["bike", "Tour"],
      ["fast", "Schnell"],
      ["safe", "Sicher"],
      ["mtb", "MTB"],
    ];
    return `<div class="route">
      <div class="route-bar">
        <button class="chip on" id="route-toggle">🗺 Route planen</button>
      </div>
      <div class="route-form">
        <input id="route-start" placeholder="Start – z. B. home oder 49.0134,12.1016"
               value="${esc(form.start)}" />
        <button class="chip" id="route-here" title="Zone 'home' einsetzen">🏠</button>
        <input id="route-destination" placeholder="Ziel – z. B. 49.02,12.15"
               value="${esc(form.destination)}" />
        <select id="route-profile">
          ${profiles
            .map(
              ([value, label]) =>
                `<option value="${value}" ${
                  value === form.profile ? "selected" : ""
                }>${label}</option>`
            )
            .join("")}
        </select>
        <label class="route-check">
          <input type="checkbox" id="route-notify" ${form.notify ? "checked" : ""} />
          ans Handy
        </label>
        <button class="chip on" id="route-go" ${this._routeBusy ? "disabled" : ""}>
          ${this._routeBusy ? "…" : "Planen"}
        </button>
        ${result ? `<button class="chip" id="route-clear">✕</button>` : ""}
      </div>
      ${
        this._routeError
          ? `<div class="error">${esc(this._routeError)}<br>
             <small>Nur Koordinaten (<code>49.0134,12.1016</code>) oder der Name
             einer Zone – eine Ortssuche gibt es nicht.</small></div>`
          : ""
      }
      ${
        result
          ? `<div class="route-result">
              <b>${fmtNum(result.distance_km, 1)} km</b> ·
              ${fmtDuration(result.duration_min * 60)} ·
              ${
                result.elevation_gain_m != null
                  ? `${Math.round(result.elevation_gain_m)} hm`
                  : "keine Höhenangabe"
              }
              <span class="backend">${esc(result.backend || "")}</span>
            </div>`
          : ""
      }
    </div>`;
  }

  _segmentList() {
    if (!this._config.show_segments || !this._segments.length) return "";
    return `<div class="chart-wrap">
      <div class="chart-title">Segmente</div>
      <div class="list">${this._segments
        .map((s) => {
          const best = s.best;
          const latest = s.latest;
          const isPb =
            best && latest && Math.abs(best.duration_s - latest.duration_s) < 0.05;
          return `<div class="row">
            <div class="ico">${isPb ? "🏆" : "📍"}</div>
            <div class="col">
              <div class="r1">${esc(s.name)}</div>
              <div class="r2">${fmtKm(s.length_m)} km · ${s.effort_count || 0} ${
                s.effort_count === 1 ? "Zeit" : "Zeiten"
              }${
                latest
                  ? ` · zuletzt ${fmtDuration(latest.duration_s)} (${fmtNum(
                      latest.avg_speed_kmh,
                      1
                    )} km/h)`
                  : " · noch keine Zeit"
              }</div>
            </div>
            <div class="km">${
              best ? fmtDuration(best.duration_s) : "–"
            }<span>best</span></div>
            <button class="chip danger" data-segment-delete="${s.id}">🗑</button>
          </div>`;
        })
        .join("")}</div>
    </div>`;
  }

  _tripList() {
    if (!this._trips.length)
      return `<div class="empty">Noch keine Fahrten aufgezeichnet.</div>`;
    return `<div class="list">${this._trips
      .map(
        (t) => `
        <div class="row ${t.id === this._selected ? "sel" : ""}" data-trip="${t.id}">
          <div class="ico">${ACTIVITY_ICON[t.activity] || "•"}</div>
          <div class="col">
            <div class="r1">${fmtDate(t.started_at)} · ${fmtTime(t.started_at)}</div>
            <div class="r2">${fmtDuration(t.moving_time_s)} · ${fmtNum(t.avg_moving_kmh, 1)} km/h · ${Math.round(t.elevation_gain_m || 0)} hm</div>
          </div>
          <div class="km">${fmtKm(t.distance_m)}<span>km</span></div>
        </div>`
      )
      .join("")}</div>`;
  }

  // ----------------------------------------------------------------- map

  _tileUrl() {
    // An explicit tile_url always wins - that is the escape hatch for a
    // self-hosted tile server.
    return this._config.tile_url || DEFAULT_TILE_URL;
  }

  async _ensureMap() {
    if (!this.shadowRoot.getElementById("map")) return null;
    let L;
    try {
      L = await loadLeaflet(this._config.leaflet_url, this._config.leaflet_css);
    } catch (err) {
      const failed = this.shadowRoot.getElementById("map");
      if (failed) failed.innerHTML = `<div class="empty">${err.message}</div>`;
      return null;
    }

    // Re-query after the await: _render() replaces the whole shadow DOM, and
    // a reference taken before would now point at a detached element. The map
    // would be built off-document, clientWidth stays 0, and the deferred
    // fitBounds never fires - an initialised map without a view or tiles.
    const slot = this.shadowRoot.getElementById("map");
    if (!slot) return null;

    // One Leaflet instance for the lifetime of the card. _render() replaces
    // the entire shadow DOM, so the map container is kept outside of it and
    // moved back into the slot afterwards. Rebuilding the map on every render
    // loses the view and leaves tiles half-positioned.
    if (!this._mapHolder) {
      this._mapHolder = document.createElement("div");
      this._mapHolder.className = "map-inner";
    }
    if (this._mapHolder.parentNode !== slot) {
      slot.textContent = "";
      slot.appendChild(this._mapHolder);
    }

    if (!this._map) {
      this._map = L.map(this._mapHolder, {
        attributionControl: true,
        zoomControl: true,
      });
      this._tileLayer = L.tileLayer(this._tileUrl(), {
        maxZoom: 19,
        attribution: `${this._config.tile_attribution || TILE_ATTRIBUTION} · Karte v${CARD_VERSION}`,
        // Home Assistant serves its frontend with a no-referrer policy, and
        // OpenStreetMap's volunteer tile servers reject requests that arrive
        // without a Referer ("Access blocked"). A policy set on the image
        // element overrides the document-level one, so the tiles send the
        // origin - which is all the tile servers ask for.
        referrerPolicy: this._config.referrer_policy,
      }).addTo(this._map);
    }

    return L;
  }

  async _drawRoute() {
    const route = this._routeResult;
    if (!route || !route.geometry || !route.geometry.length) return;
    const L = await this._ensureMap();
    if (!L) return;
    if (this._routeLayer) this._routeLayer.remove();
    this._routeLayer = L.layerGroup([
      L.polyline(route.geometry, {
        color: "#ff9800",
        weight: 4,
        opacity: 0.9,
        dashArray: "6 6",
      }),
      L.circleMarker(route.geometry[0], {
        radius: 5,
        color: "#ff9800",
        fillOpacity: 1,
      }),
    ]).addTo(this._map);
    this._fit(L.polyline(route.geometry).getBounds());
  }

  _afterLayout(action) {
    // Size first, then position. Leaflet caches the container size; running
    // fitBounds/setView against a stale one picks the wrong zoom and leaves
    // the tile grid half empty.
    //
    // Deliberately not requestAnimationFrame alone: in a background tab the
    // browser stops firing it, and the card would sit there with an unsized
    // map until the tab is looked at. A ResizeObserver plus plain timeouts
    // works whether or not the page is compositing.
    this._pendingLayout = action;

    const run = () => {
      if (!this._map || !this._pendingLayout) return;
      if (!this._mapHolder || this._mapHolder.clientWidth === 0) return;
      this._map.invalidateSize({ animate: false });
      const pending = this._pendingLayout;
      this._pendingLayout = null;
      pending();
    };

    if (!this._resizeObserver && this._mapHolder && window.ResizeObserver) {
      this._resizeObserver = new ResizeObserver(() => {
        if (this._map) this._map.invalidateSize({ animate: false });
        run();
      });
      this._resizeObserver.observe(this._mapHolder);
    }

    run();
    setTimeout(run, 0);
    setTimeout(run, 300);
  }

  _fit(bounds) {
    this._afterLayout(() => this._map.fitBounds(bounds, { padding: [20, 20] }));
  }

  _centreOnHome() {
    // Nothing to show yet - centre on the Home Assistant location so a fresh
    // install shows a map instead of an empty grey box.
    if (this._routeResult) return;
    const cfg = (this._hass && this._hass.config) || {};
    const home =
      typeof cfg.latitude === "number" && typeof cfg.longitude === "number"
        ? [[cfg.latitude, cfg.longitude], 13]
        : [[51.163, 10.448], 6];
    this._afterLayout(() => this._map.setView(home[0], home[1]));
  }

  async _drawMap() {
    const L = await this._ensureMap();
    if (!L) return;

    const hasTrack =
      this._detail && this._detail.track && this._detail.track.length;
    if (!hasTrack) {
      if (this._layer) {
        this._layer.remove();
        this._layer = null;
      }
      this._centreOnHome();
      return;
    }

    if (this._layer) this._layer.remove();
    const track = this._detail.track;
    this._layer = L.layerGroup([
      L.polyline(track, { color: "#03a9f4", weight: 4, opacity: 0.9 }),
      L.circleMarker(track[0], { radius: 6, color: "#43a047", fillOpacity: 1 }),
      L.circleMarker(track[track.length - 1], {
        radius: 6,
        color: "#e53935",
        fillOpacity: 1,
      }),
    ]).addTo(this._map);
    this._fit(L.polyline(track).getBounds());
  }
}

const STYLES = `
  ha-card { padding: 12px 12px 16px; }
  .header { display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:8px; }
  .title { font-size:1.25rem; font-weight:600; }
  .tabs { display:flex; gap:4px; flex-wrap:wrap; }
  .tab { border:none; background:var(--secondary-background-color); color:var(--primary-text-color);
         border-radius:14px; padding:4px 10px; font-size:.8rem; cursor:pointer; }
  .tab.active { background:var(--primary-color); color:var(--text-primary-color,#fff); }
  .kpis, .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(88px,1fr)); gap:8px; margin:12px 0; }
  .kpi { background:var(--secondary-background-color); border-radius:10px; padding:8px; text-align:center; }
  .kpi .v { font-size:1.05rem; font-weight:600; }
  .kpi .l { font-size:.7rem; opacity:.7; margin-top:2px; }
  /* Höhe kommt per Inline-Style aus map_height, das hier ist nur der Rückfall. */
  .map { height:500px; border-radius:10px; overflow:hidden; background:var(--secondary-background-color); }
  .map-inner { width:100%; height:100%; }
  /* Leaflet draws on a dark card - keep its chrome readable. */
  .leaflet-container { background:var(--secondary-background-color); font-family:inherit; }
  .leaflet-control-attribution { font-size:.62rem; }
  .chart-wrap { margin:12px 0; }
  .chart-title { font-size:.72rem; opacity:.7; margin-bottom:4px; }
  .bars { position:relative; height:70px; border-bottom:1px solid var(--divider-color); }
  .bar { position:absolute; bottom:0; background:var(--primary-color); border-radius:2px 2px 0 0; min-height:1px; }
  .elev { width:100%; height:70px; }
  .elev polygon { fill:var(--primary-color); opacity:.18; }
  .elev polyline { fill:none; stroke:var(--primary-color); stroke-width:1.4; vector-effect:non-scaling-stroke; }
  .detail { margin:12px 0; }
  .detail-head { display:flex; justify-content:space-between; align-items:flex-start; gap:8px; flex-wrap:wrap; }
  .dt { font-weight:600; }
  .sub { font-size:.75rem; opacity:.7; }
  .actions { display:flex; gap:4px; align-items:center; }
  .chip { border:1px solid var(--divider-color); background:transparent; color:var(--primary-text-color);
          border-radius:8px; padding:3px 8px; font-size:.85rem; cursor:pointer; text-decoration:none; }
  .chip.on { background:var(--primary-color); border-color:var(--primary-color); color:var(--text-primary-color,#fff); }
  .chip.danger:hover { border-color:var(--error-color,#e53935); }
  .list { margin-top:8px; max-height:340px; overflow-y:auto; }
  .row { display:flex; align-items:center; gap:10px; padding:8px 6px; border-radius:8px; cursor:pointer; }
  .row:hover { background:var(--secondary-background-color); }
  .row.sel { background:var(--secondary-background-color); box-shadow:inset 3px 0 0 var(--primary-color); }
  .ico { font-size:1.15rem; width:24px; text-align:center; }
  .col { flex:1; min-width:0; }
  .r1 { font-size:.85rem; }
  .r2 { font-size:.72rem; opacity:.68; }
  .km { font-weight:600; font-size:1rem; }
  .km span { font-size:.65rem; opacity:.6; margin-left:2px; }
  .empty { padding:18px; text-align:center; opacity:.6; font-size:.85rem; }
  .error { background:var(--error-color,#e53935); color:#fff; padding:8px; border-radius:8px; font-size:.8rem; margin:8px 0; }
  .live { display:flex; align-items:center; gap:8px; background:var(--primary-color);
          color:var(--text-primary-color,#fff); padding:6px 10px; border-radius:8px; margin:8px 0; font-size:.85rem; }
  .live .dot { width:8px; height:8px; border-radius:50%; background:#fff; animation:pulse 1.4s infinite; }
  .live .speed { margin-left:auto; font-weight:600; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.25} }
  .route { margin:8px 0 12px; }
  .route-bar { display:flex; align-items:center; gap:8px; margin:8px 0; }
  .route-sum { font-size:.78rem; opacity:.7; }
  .route-form { display:flex; flex-wrap:wrap; gap:6px; align-items:center; }
  .route-form input[type=text], .route-form input:not([type]) {
    flex:1 1 150px; min-width:0; }
  .route-form input, .route-form select {
    background:var(--secondary-background-color); color:var(--primary-text-color);
    border:1px solid var(--divider-color); border-radius:8px; padding:5px 8px;
    font-size:.85rem; font-family:inherit; }
  .route-check { display:flex; align-items:center; gap:4px; font-size:.78rem; opacity:.8; }
  .route-check input { flex:none; }
  .route-result { margin-top:8px; font-size:.85rem; }
  .route-result .backend { font-size:.68rem; opacity:.55; margin-left:6px; }
  .chip[disabled] { opacity:.5; cursor:default; }
`;

customElements.define("bike-tracker-card", BikeTrackerCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "bike-tracker-card",
  name: "Bike Tracker",
  description: "Fahrten, Karte, Höhenprofil und Statistik des Bike Trackers",
  preview: false,
});

console.info(
  `%c BIKE-TRACKER-CARD %c ${CARD_VERSION} `,
  "background:#03a9f4;color:#fff",
  ""
);
