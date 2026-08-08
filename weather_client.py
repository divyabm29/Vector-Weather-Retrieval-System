"""Client for the National Weather Service (NWS) API.

Fetches weather alerts and forecasts from weather.gov.
No API key required - the NWS API is public.
"""

import hashlib
import re
from typing import Any

import requests

_BASE_URL = "https://api.weather.gov"
_DEFAULT_TIMEOUT = 30

# User-Agent is required by NWS API terms
_USER_AGENT = "DatabricksWeatherApp/1.0"


class WeatherClient:
    """Thin wrapper around the National Weather Service API."""

    def __init__(self, base_url: str | None = None, timeout: int = _DEFAULT_TIMEOUT):
        self.base_url = (base_url or _BASE_URL).rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": _USER_AGENT,
                "Accept": "application/geo+json",
            }
        )

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Make a GET request to the NWS API."""
        resp = self._session.get(f"{self.base_url}{path}", params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def resolve_gridpoint(self, lat: float, lon: float) -> dict:
        """Resolve a lat/lon to NWS grid coordinates.
        
        Returns: {"gridId": "...", "gridX": N, "gridY": M, "forecast": "url", ...}
        """
        data = self.get(f"/points/{lat:.4f},{lon:.4f}")
        return data.get("properties", {})

    def get_active_alerts(self, lat: float, lon: float, limit: int = 50) -> list[dict]:
        """Fetch active weather alerts for a specific location.
        
        Returns a list of alert features from the NWS alerts API.
        Each alert has properties: id, event, headline, description, instruction, ...
        """
        params = {
            "point": f"{lat:.4f},{lon:.4f}",
            "status": "actual",
            "message_type": "alert",
            "limit": limit,
        }
        data = self.get("/alerts/active", params=params)
        return data.get("features", [])

    def get_forecast(self, gridpoint_forecast_url: str) -> list[dict]:
        """Fetch the detailed forecast periods from a gridpoint forecast URL.
        
        The forecast URL comes from resolve_gridpoint()["forecast"].
        Returns a list of forecast periods with: number, name, temperature, 
        detailedForecast, ...
        """
        # The forecast URL is already a full URL, just fetch it
        resp = self._session.get(gridpoint_forecast_url, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        return data.get("properties", {}).get("periods", [])

    def get_forecast_discussion(self, grid_id: str) -> dict | None:
        """Fetch the forecast discussion (AFD - Area Forecast Discussion) for a grid.
        
        Returns the discussion text and issuance time, or None if not available.
        """
        try:
            # Discussions are at /gridpoints/{wfo}/{x},{y}/forecast/discussion
            # But we just have grid_id (office), so fetch the office's discussions
            data = self.get(f"/products/types/AFD/locations/{grid_id}")
            products = data.get("@graph", [])
            if not products:
                return None
            
            # Get the most recent discussion
            product_url = products[0].get("@id", "")
            if not product_url:
                return None
            
            # Fetch the full discussion text
            discussion_data = self.get(product_url.replace(self.base_url, ""))
            return discussion_data.get("properties", {})
        except requests.HTTPError:
            return None


def parse_location(location_str: str) -> tuple[float, float] | None:
    """Parse a location string into (lat, lon).
    
    Supports:
    - "lat,lon" format: "41.8781,-87.6298"
    - City/state is NOT supported here - caller should geocode separately
    
    Returns (lat, lon) or None if parsing fails.
    """
    location_str = location_str.strip()
    
    # Try lat,lon format
    match = re.match(r"^(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)$", location_str)
    if match:
        lat, lon = float(match.group(1)), float(match.group(2))
        return (lat, lon)
    
    return None


def normalize_alert(alert_feature: dict, location: str) -> dict:
    """Normalize an NWS alert feature into a document record.
    
    Returns a dict with: id, location, source_type, headline, event, 
    narrative_text, issued_at, effective_at, payload, synced_at (placeholder).
    """
    props = alert_feature.get("properties", {})
    alert_id = props.get("id", "")
    
    return {
        "id": alert_id,
        "location": location,
        "source_type": "alert",
        "headline": props.get("headline", ""),
        "event": props.get("event", ""),
        "narrative_text": "\n\n".join(
            filter(None, [
                props.get("description", ""),
                props.get("instruction", ""),
            ])
        ),
        "issued_at": props.get("sent"),
        "effective_at": props.get("effective"),
        "payload": alert_feature,
    }


def normalize_forecast(period: dict, location: str, gridpoint: dict) -> dict:
    """Normalize a forecast period into a document record.
    
    Returns a dict with: id, location, source_type, headline, event, 
    narrative_text, issued_at, effective_at, payload, synced_at (placeholder).
    """
    # Generate a stable ID from location + period number + forecast update time
    forecast_url = gridpoint.get("forecast", "")
    period_num = period.get("number", 0)
    period_name = period.get("name", "")
    start_time = period.get("startTime", "")
    
    id_source = f"{forecast_url}:{period_num}:{start_time}"
    doc_id = hashlib.sha256(id_source.encode()).hexdigest()[:32]
    
    return {
        "id": doc_id,
        "location": location,
        "source_type": "forecast",
        "headline": f"{period_name}: {period.get('shortForecast', '')}",
        "event": period_name,
        "narrative_text": period.get("detailedForecast", ""),
        "issued_at": start_time,
        "effective_at": start_time,
        "payload": period,
    }


def normalize_discussion(discussion: dict, location: str, grid_id: str) -> dict | None:
    """Normalize a forecast discussion into a document record.
    
    Returns a dict with: id, location, source_type, headline, event, 
    narrative_text, issued_at, effective_at, payload, synced_at (placeholder).
    Or None if discussion is empty.
    """
    if not discussion:
        return None
    
    discussion_id = discussion.get("id", "")
    if not discussion_id:
        return None
    
    issued = discussion.get("issuanceTime", "")
    product_text = discussion.get("productText", "")
    
    return {
        "id": discussion_id,
        "location": location,
        "source_type": "discussion",
        "headline": f"Forecast Discussion - {grid_id}",
        "event": "Area Forecast Discussion",
        "narrative_text": product_text,
        "issued_at": issued,
        "effective_at": issued,
        "payload": discussion,
    }
