"""
Open-Meteo weather + elevation fetcher.
Free, no API key required.
"""

import httpx
from typing import Optional
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from engine.pace_engine import WeatherData


WEATHER_URL   = "https://api.open-meteo.com/v1/forecast"
ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"


async def fetch_weather(lat: float, lng: float) -> WeatherData:
    """
    Fetches current weather from Open-Meteo for the given coordinates.
    Returns WeatherData with sensible defaults on failure.
    """
    params = {
        "latitude":  lat,
        "longitude": lng,
        "current": [
            "temperature_2m",
            "apparent_temperature",
            "relative_humidity_2m",
            "wind_speed_10m",
            "wind_direction_10m",
            "precipitation",
        ],
        "wind_speed_unit": "ms",
        "forecast_days": 1,
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(WEATHER_URL, params=params)
            r.raise_for_status()
            c = r.json()["current"]
            return WeatherData(
                temperature_c    = c.get("temperature_2m", 20.0),
                apparent_temp_c  = c.get("apparent_temperature", 20.0),
                humidity_pct     = c.get("relative_humidity_2m", 60.0),
                wind_speed_ms    = c.get("wind_speed_10m", 0.0),
                wind_dir_deg     = c.get("wind_direction_10m", 0.0),
                precipitation_mm = c.get("precipitation", 0.0),
            )
    except Exception:
        return WeatherData()   # neutral defaults


async def fetch_elevation(lat: float, lng: float) -> Optional[float]:
    """Returns elevation in meters for the coordinate, or None on error."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                ELEVATION_URL,
                params={"latitude": lat, "longitude": lng},
            )
            r.raise_for_status()
            results = r.json().get("elevation", [])
            return results[0] if results else None
    except Exception:
        return None
