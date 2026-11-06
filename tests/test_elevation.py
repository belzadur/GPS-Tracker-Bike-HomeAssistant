"""DEM elevation lookup: sampling, batching, caching, interpolation."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import pytest
from bt.elevation import (
    MAX_LOCATIONS_PER_REQUEST,
    ElevationCache,
    ElevationError,
    async_elevations_for_track,
    async_lookup,
    build_request,
    interpolate,
    parse_response,
    sample_indices,
)


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    """Drive one coroutine to completion - keeps the suite plugin-free."""
    return asyncio.run(coro)


def line(count: int, step_deg: float = 0.001) -> list[tuple[float, float]]:
    """A straight track heading east. 0.001 deg lon ~ 68 m at 49 deg N."""
    return [(49.0, 12.0 + i * step_deg) for i in range(count)]


class FakeBackend:
    """Records every call and answers with a fixed elevation."""

    def __init__(self, elevation: float = 400.0) -> None:
        self.elevation = elevation
        self.calls: list[tuple[str, str, dict | None]] = []

    async def __call__(self, method: str, url: str, body: dict | None):
        self.calls.append((method, url, body))
        if body is not None:
            count = len(body["locations"])
        else:
            count = len(url.split("locations=")[1].split("|"))
        return {"status": "OK", "results": [{"elevation": self.elevation}] * count}


# --- sampling -----------------------------------------------------------


def test_sample_indices_thins_out_dense_tracks():
    track = line(50, step_deg=0.0001)  # ~7 m spacing

    indices = sample_indices(track, min_distance_m=25.0)

    assert indices[0] == 0
    assert indices[-1] == len(track) - 1
    assert len(indices) < len(track)


def test_sample_indices_keeps_everything_when_points_are_far_apart():
    track = line(5, step_deg=0.01)  # ~700 m spacing

    assert sample_indices(track, min_distance_m=25.0) == [0, 1, 2, 3, 4]


def test_sample_indices_handles_short_tracks():
    assert sample_indices([]) == []
    assert sample_indices([(49.0, 12.0)]) == [0]
    assert sample_indices([(49.0, 12.0), (49.0, 12.1)]) == [0, 1]


# --- interpolation ------------------------------------------------------


def test_interpolate_fills_the_gaps_linearly():
    result = interpolate(5, [0, 4], [100.0, 200.0])

    assert result == [100.0, 125.0, 150.0, 175.0, 200.0]


def test_interpolate_extrapolates_flat_beyond_the_samples():
    result = interpolate(5, [1, 3], [100.0, 300.0])

    assert result[0] == 100.0
    assert result[4] == 300.0


def test_interpolate_survives_a_backend_that_answered_nothing():
    assert interpolate(3, [0, 2], [None, None]) == [None, None, None]


def test_interpolate_with_a_single_known_value_is_flat():
    assert interpolate(3, [0, 2], [None, 250.0]) == [250.0, 250.0, 250.0]


# --- request building ---------------------------------------------------


def test_build_request_uses_get_for_opentopodata():
    method, url, body = build_request(
        "https://api.opentopodata.org/v1/eudem25m", [(49.0, 12.0), (49.1, 12.1)]
    )

    assert method == "GET"
    assert body is None
    assert "locations=49.000000,12.000000|49.100000,12.100000" in url


def test_build_request_uses_post_for_open_elevation():
    method, url, body = build_request(
        "https://api.open-elevation.com", [(49.0, 12.0)]
    )

    assert method == "POST"
    assert url.endswith("/api/v1/lookup")
    assert body == {"locations": [{"latitude": 49.0, "longitude": 12.0}]}


def test_build_request_does_not_double_the_lookup_path():
    _method, url, _body = build_request(
        "https://my-host/api/v1/lookup", [(49.0, 12.0)]
    )

    assert url == "https://my-host/api/v1/lookup"


# --- response parsing ---------------------------------------------------


def test_parse_response_reads_elevations():
    payload = {"status": "OK", "results": [{"elevation": 412.5}, {"elevation": 9.0}]}

    assert parse_response(payload, 2) == [412.5, 9.0]


def test_parse_response_maps_null_elevation_to_none():
    payload = {"results": [{"elevation": None}]}

    assert parse_response(payload, 1) == [None]


def test_parse_response_rejects_a_truncated_answer():
    with pytest.raises(ElevationError):
        parse_response({"results": [{"elevation": 1.0}]}, 2)


def test_parse_response_rejects_an_error_status():
    with pytest.raises(ElevationError):
        parse_response({"status": "INVALID_REQUEST", "results": []}, 0)


def test_parse_response_rejects_garbage():
    with pytest.raises(ElevationError):
        parse_response("not json at all", 1)


# --- lookup, batching, caching -----------------------------------------


def test_lookup_splits_into_batches():
    backend = FakeBackend()
    coords = line(MAX_LOCATIONS_PER_REQUEST * 2 + 5, step_deg=0.01)

    values = run(async_lookup(backend, "https://dem/v1/x", coords, request_delay_s=0))

    assert len(values) == len(coords)
    assert len(backend.calls) == 3


def test_lookup_serves_repeats_from_the_cache():
    backend = FakeBackend()
    cache = ElevationCache()
    coords = line(10, step_deg=0.01)

    run(async_lookup(backend, "https://dem/v1/x", coords, cache, request_delay_s=0))
    calls_after_first = len(backend.calls)
    values = run(
        async_lookup(backend, "https://dem/v1/x", coords, cache, request_delay_s=0)
    )

    assert len(backend.calls) == calls_after_first  # nothing new went out
    assert values == [400.0] * 10


def test_elevations_for_track_returns_one_value_per_point():
    backend = FakeBackend(elevation=333.0)
    track = line(200, step_deg=0.0001)

    values = run(
        async_elevations_for_track(
            backend, "https://dem/v1/x", track, request_delay_s=0
        )
    )

    assert len(values) == len(track)
    assert set(values) == {333.0}
    # The whole track is ~1.4 km, so far fewer than 200 points were queried.
    assert len(backend.calls) == 1


def test_elevations_for_track_handles_an_empty_track():
    assert run(async_elevations_for_track(FakeBackend(), "https://dem", [])) == []
