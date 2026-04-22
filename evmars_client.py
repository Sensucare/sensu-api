"""
EVMars API Client

Handles communication with the EVMars/LocTube cloud API for device property
configuration (fall detection, geofencing, battery thresholds, etc.).
"""

import os
import logging
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)


class EVMarsClient:
    """Client for the EVMars/LocTube device management API."""

    def __init__(
        self,
        api_url: Optional[str] = None,
        client_id: Optional[str] = None,
        secure_key: Optional[str] = None,
        timeout: int = 10,
    ):
        self.api_url = api_url or os.getenv('EVMARS_API_URL', 'http://test-loctube-api.katchu.cn')
        self.client_id = client_id or os.getenv('EVMARS_CLIENT_ID', 'Dj4RsEe2xk8YGpTb')
        self.secure_key = secure_key or os.getenv('EVMARS_SECURE_KEY', 'jk5xSAPHQPtTWD2yaZpQGxH7')
        self.timeout = timeout

    def _get_headers(self) -> Dict[str, str]:
        return {
            'S-Client-Id': self.client_id,
            'S-Secure-Key': self.secure_key,
            'Content-Type': 'application/json',
        }

    def _request(self, method: str, path: str, data: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
        """Make an HTTP request to the EVMars API."""
        url = f"{self.api_url}{path}"
        try:
            response = requests.request(
                method=method,
                url=url,
                headers=self._get_headers(),
                json=data,
                timeout=self.timeout,
            )
            if response.status_code == 200:
                return response.json()
            else:
                logger.warning(f"EVMars API {method} {path} returned {response.status_code}: {response.text}")
                return None
        except requests.Timeout:
            logger.error(f"EVMars API timeout: {method} {path}")
            return None
        except Exception as e:
            logger.error(f"EVMars API error: {method} {path} - {e}")
            return None

    # ─── Device Status ───────────────────────────────────────────────────────────

    def get_device_realtime(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Fetch real-time device status (location, battery, signal)."""
        return self._request('GET', f'/device/evgps/{device_id}/realtime')

    def get_device_property(self, device_id: str, property_name: str) -> Optional[Dict[str, Any]]:
        """Read a specific device property."""
        return self._request('GET', f'/api/v1/device/{device_id}/property/{property_name}')

    def get_geofence_zones(self, device_id: str) -> Optional[list]:
        """
        Read all geofence zones from the device.

        The API only supports reading via 'geoAlert' (not geoAlert2/3/4).
        Returns a list of 4 zone dicts with: flag, index, points, status,
        direction, type, radius, latlng.
        """
        result = self.get_device_property(device_id, "geoAlert")
        if result and "result" in result:
            return result["result"].get("geoAlert", [])
        return None

    def get_geofence_zone(self, device_id: str, zone_number: int) -> Optional[Dict[str, Any]]:
        """Read a specific geofence zone (1-4) from the device."""
        zones = self.get_geofence_zones(device_id)
        if zones:
            index = max(0, min(3, zone_number - 1))
            for zone in zones:
                if zone.get("index") == index:
                    return zone
        return None

    # ─── Device Properties (Write) ──────────────────────────────────────────────

    def set_device_properties(self, device_id: str, properties: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Write one or more properties to a device.
        POST /api/v1/device/{deviceId}/properties
        """
        return self._request('POST', f'/api/v1/device/{device_id}/properties', data=properties)

    # ─── Fall Detection Configuration ────────────────────────────────────────────

    def configure_fall_detection(
        self,
        device_id: str,
        enabled: bool,
        sensitivity: int = 1,
        dial: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """
        Configure fall detection on the device via fallDownAlert property (0x56).

        The API expects explicit fields and internally computes the packed flag.

        Args:
            device_id: Device identifier
            enabled: Whether to enable fall detection
            sensitivity: Sensitivity level 1-9 (1=least sensitive, 9=most sensitive)
            dial: Whether device should call SOS numbers on fall detection
        """
        sensitivity = max(1, min(9, sensitivity))

        payload = {
            "fallDownAlert": {
                "level": sensitivity,
                "status": 1 if enabled else 0,
                "dial": 1 if dial else 0,
                "allwayOn": 0,
            }
        }

        logger.info(f"Configuring fall detection for {device_id}: enabled={enabled}, "
                    f"sensitivity={sensitivity}, dial={dial}")

        return self.set_device_properties(device_id, payload)

    # ─── Geofence Configuration ──────────────────────────────────────────────────

    def configure_geofence(
        self,
        device_id: str,
        zone_number: int,
        center_lat: float,
        center_lng: float,
        radius_meters: float,
        direction: str = "out",
        enabled: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """
        Configure a circle geofence on the device via geoAlert property (0x51).

        The EVMars API accepts geoAlert, geoAlert2, geoAlert3, geoAlert4 for zones 1-4.

        The API expects explicit fields (index, status, direction, type, radius, latlng)
        and internally computes the packed flag integer.

        Args:
            device_id: Device identifier
            zone_number: Geofence zone 1-4
            center_lat: Center latitude (decimal degrees)
            center_lng: Center longitude (decimal degrees)
            radius_meters: Radius in meters (max 65535)
            direction: 'in' (enter alert), 'out' (exit alert), or 'both'
            enabled: Whether the geofence is active
        """
        zone_number = max(1, min(4, zone_number))
        radius_meters = max(1, min(65535, int(radius_meters)))
        index = zone_number - 1

        # Direction mapping: 0=exit alert, 1=enter alert
        # For 'both', default to exit alert (most common for elderly safety)
        direction_value = 1 if direction == "in" else 0

        # Property name maps: zone 1 = geoAlert, zone 2 = geoAlert2, etc.
        property_name = "geoAlert" if zone_number == 1 else f"geoAlert{zone_number}"

        payload = {
            property_name: {
                "flag": index,
                "index": index,
                "points": 1,
                "status": 1 if enabled else 0,
                "direction": direction_value,
                "type": 0,  # 0=circle
                "radius": radius_meters,
                "latlng": [
                    {"lat": center_lat, "lng": center_lng}
                ],
            }
        }

        logger.info(f"Configuring geofence zone {zone_number} for {device_id}: "
                    f"center=({center_lat}, {center_lng}), radius={radius_meters}m, "
                    f"direction={direction}, enabled={enabled}")

        return self.set_device_properties(device_id, payload)

    def disable_geofence(self, device_id: str, zone_number: int) -> Optional[Dict[str, Any]]:
        """Disable a geofence zone on the device."""
        zone_number = max(1, min(4, zone_number))
        index = zone_number - 1

        property_name = "geoAlert" if zone_number == 1 else f"geoAlert{zone_number}"

        payload = {
            property_name: {
                "flag": index,
                "index": index,
                "points": 0,
                "status": 0,
                "direction": 0,
                "type": 0,
                "radius": 0,
                "latlng": [],
            }
        }

        logger.info(f"Disabling geofence zone {zone_number} for {device_id}")
        return self.set_device_properties(device_id, payload)

    def configure_geo_detect_interval(
        self,
        device_id: str,
        interval_seconds: int,
        enabled: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """
        Configure geofence detection interval (0x5A for EV04/EV05).

        How often the device checks if it's inside/outside geofences.

        Args:
            device_id: Device identifier
            interval_seconds: Check interval in seconds (60-86400, default 180)
            enabled: Whether detection is active
        """
        interval_seconds = max(60, min(86400, interval_seconds))

        # Bit 31 = enable flag, bits 0-30 = interval
        value = interval_seconds & 0x7FFFFFFF
        if enabled:
            value |= (1 << 31)

        logger.info(f"Configuring geo detect interval for {device_id}: {interval_seconds}s, enabled={enabled}")
        return self.set_device_properties(device_id, {"geoAlertDetectSettings": value})

    # ─── Contact Number Management ──────────────────────────────────────────────

    def get_contact_numbers(self, device_id: str) -> Optional[list]:
        """
        Read all authorized contact numbers from the device.
        Returns a list of dicts with: index, number, enabled, call, sms, flag.
        """
        result = self.get_device_property(device_id, "number")
        if result and "number" in result:
            return result["number"]
        return None

    def set_contact_number(
        self,
        device_id: str,
        index: int,
        number: str,
        enabled: bool = True,
        call: bool = True,
        sms: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """
        Set a single authorized contact number on the device.

        Uses the expanded format with explicit index field, which is required
        for the device to correctly route the number to the right slot.

        Args:
            device_id: Device IMEI
            index: Contact slot 0-9
            number: Phone number string
            enabled: Whether this contact is active
            call: Device can dial this number
            sms: Device accepts SMS from this number
        """
        index = max(0, min(9, index))

        flag = index & 0x0F
        if call:
            flag |= (1 << 5)
        if sms:
            flag |= (1 << 6)
        if enabled:
            flag |= (1 << 7)

        payload = {
            "number": {
                "flag": flag,
                "enable": 1 if enabled else 0,
                "sms": 1 if sms else 0,
                "call": 1 if call else 0,
                "noCard": 0,
                "index": index,
                "number": number,
            }
        }

        logger.info(f"Setting contact number for {device_id}: "
                    f"index={index}, number={number}, enabled={enabled}")

        return self.set_device_properties(device_id, payload)

    def delete_contact_number(self, device_id: str, index: int) -> Optional[Dict[str, Any]]:
        """Clear a contact number slot on the device."""
        index = max(0, min(9, index))

        payload = {
            "number": {
                "flag": index & 0x0F,
                "enable": 0,
                "sms": 0,
                "call": 0,
                "noCard": 0,
                "index": index,
                "number": "",
            }
        }

        logger.info(f"Clearing contact number for {device_id}: index={index}")
        return self.set_device_properties(device_id, payload)

    # ─── Device Commands ─────────────────────────────────────────────────────────

    def execute_function(self, device_id: str, function_id: str) -> Optional[Dict[str, Any]]:
        """
        Execute a device function (findMe, singleLocating, etc.).
        POST /device/instance/{deviceId}/function/{functionId}
        """
        return self._request(
            'POST',
            f'/device/instance/{device_id}/function/{function_id}',
            data={"deviceId": device_id, "functionId": function_id},
        )

    def find_device(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Make the device beep/vibrate to locate it."""
        return self.execute_function(device_id, "findMe")

    def request_location(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Request an immediate location update from the device."""
        return self.execute_function(device_id, "singleLocating")


# Singleton instance
_evmars_client: Optional[EVMarsClient] = None


def get_evmars_client() -> EVMarsClient:
    """Get or create the singleton EVMars client instance."""
    global _evmars_client
    if _evmars_client is None:
        _evmars_client = EVMarsClient()
    return _evmars_client
