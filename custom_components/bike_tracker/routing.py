"""Route planning - scaffold.

The plumbing (service, event, HTTP endpoint, push to the phone) is complete;
only the routing backend itself is intentionally minimal for v1. Two backends
are wired up behind one interface:

* ``osrm``    - the public OSRM demo server (car profile only, no API key)
* ``brouter`` - a self-hosted BRouter instance, which has proper cycling
                profiles ("trekking", "fastbike", "safety"). This is the one
                to use once you host it; set the URL in the options flow.

Both return the same normalised dict, so the frontend and the notify payload
do not change when the backend does.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_ROUTING_URL, DEFAULT_ROUTING_URL, EVENT_ROUTE_PLANNED

if TYPE_CHECKING:
    from .coordinator import BikeTrackerCoordinator

_LOGGER = logging.getLogger(__name__)

BROUTER_PROFILES = {
    "bike": "trekking",
    "fast": "fastbike",
    "safe": "safety",
    "mtb": "mtb",
}


async def async_plan_route(
    hass: HomeAssistant,
    coordinator: BikeTrackerCoordinator,
    start: str,
    destination: str,
    profile: str = "bike",
    notify_device: str | None = None,
) -> dict[str, Any]:
    """Plan a route between two ``lat,lon`` pairs or zone names."""
    origin = _resolve(hass, start)
    target = _resolve(hass, destination)
    if origin is None or target is None:
        raise HomeAssistantError(
            "start and destination must be 'lat,lon' or the name of a zone"
        )

    base_url = str(
        coordinator.options.get(CONF_ROUTING_URL, DEFAULT_ROUTING_URL)
    ).rstrip("/")
    session = async_get_clientsession(hass)

    try:
        if "brouter" in base_url:
            route = await _brouter(session, base_url, origin, target, profile)
        else:
            route = await _osrm(session, base_url, origin, target)
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        # Server down, slow or answering garbage: a clear message for the
        # card and the service caller instead of a traceback.
        raise HomeAssistantError(
            f"Routing backend {base_url} not reachable: {err}"
        ) from err

    route["start"] = list(origin)
    route["destination"] = list(target)
    route["profile"] = profile

    hass.bus.async_fire(
        EVENT_ROUTE_PLANNED,
        {k: v for k, v in route.items() if k != "geometry"},
    )

    if notify_device:
        await _push_to_phone(hass, notify_device, route)

    return route


def _resolve(hass: HomeAssistant, value: str) -> tuple[float, float] | None:
    """Accept 'lat,lon', a zone entity id, or a zone friendly name."""
    text = value.strip()
    if "," in text:
        try:
            lat_text, lon_text = text.split(",", 1)
            return float(lat_text), float(lon_text)
        except ValueError:
            return None

    state = hass.states.get(text if "." in text else f"zone.{text.lower()}")
    if state is None:
        for candidate in hass.states.async_all("zone"):
            if candidate.name.lower() == text.lower():
                state = candidate
                break
    if state is None:
        return None
    lat = state.attributes.get("latitude")
    lon = state.attributes.get("longitude")
    if lat is None or lon is None:
        return None
    return float(lat), float(lon)


async def _osrm(
    session: Any,
    base_url: str,
    origin: tuple[float, float],
    target: tuple[float, float],
) -> dict[str, Any]:
    url = (
        f"{base_url}/route/v1/driving/"
        f"{origin[1]},{origin[0]};{target[1]},{target[0]}"
        "?overview=full&geometries=geojson&steps=false"
    )
    async with session.get(url, timeout=30) as response:
        if response.status != 200:
            raise HomeAssistantError(f"Routing backend returned {response.status}")
        payload = await response.json()

    if payload.get("code") != "Ok" or not payload.get("routes"):
        raise HomeAssistantError(f"No route found: {payload.get('code')}")

    route = payload["routes"][0]
    coords = route["geometry"]["coordinates"]
    return {
        "backend": "osrm",
        "distance_km": round(route["distance"] / 1000.0, 2),
        "duration_min": round(route["duration"] / 60.0, 1),
        "elevation_gain_m": None,
        "geometry": [[lat, lon] for lon, lat in coords],
    }


async def _brouter(
    session: Any,
    base_url: str,
    origin: tuple[float, float],
    target: tuple[float, float],
    profile: str,
) -> dict[str, Any]:
    brouter_profile = BROUTER_PROFILES.get(profile, "trekking")
    url = (
        f"{base_url}/brouter"
        f"?lonlats={origin[1]},{origin[0]}|{target[1]},{target[0]}"
        f"&profile={brouter_profile}&alternativeidx=0&format=geojson"
    )
    async with session.get(url, timeout=45) as response:
        if response.status != 200:
            raise HomeAssistantError(f"BRouter returned {response.status}")
        payload = await response.json()

    features = payload.get("features") or []
    if not features:
        raise HomeAssistantError("BRouter returned no route")
    feature = features[0]
    props = feature.get("properties", {})
    coords = feature["geometry"]["coordinates"]
    return {
        "backend": "brouter",
        "distance_km": round(float(props.get("track-length", 0)) / 1000.0, 2),
        "duration_min": round(float(props.get("total-time", 0)) / 60.0, 1),
        "elevation_gain_m": float(props.get("filtered ascend", 0) or 0),
        "geometry": [[c[1], c[0]] for c in coords],
    }


async def _push_to_phone(
    hass: HomeAssistant, notify_device: str, route: dict[str, Any]
) -> None:
    """Send the planned route to the companion app as an actionable notice."""
    service = notify_device.replace("notify.", "")
    if not hass.services.has_service("notify", service):
        _LOGGER.warning("notify.%s does not exist - skipping route push", service)
        return

    start = route["start"]
    end = route["destination"]
    await hass.services.async_call(
        "notify",
        service,
        {
            "title": "Route planned",
            "message": (
                f"{route['distance_km']} km, about {route['duration_min']:.0f} min"
            ),
            "data": {
                "actions": [
                    {
                        "action": "URI",
                        "title": "Open in maps",
                        "uri": (
                            "https://www.google.com/maps/dir/?api=1"
                            f"&origin={start[0]},{start[1]}"
                            f"&destination={end[0]},{end[1]}"
                            "&travelmode=bicycling"
                        ),
                    }
                ]
            },
        },
        blocking=False,
    )
