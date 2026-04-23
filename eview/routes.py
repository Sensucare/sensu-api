import datetime
import logging
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Depends

from auth.core import get_current_user
from eview.models import (
    DeviceAssociation, LinkDeviceRequest, EviewStatus, EviewEvent, EviewMQTTStatus,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def get_user_manager():
    from watch_app import user_manager
    return user_manager


def get_eview_event_manager():
    from watch_app import eview_event_manager
    return eview_event_manager


def get_db_manager():
    from watch_app import db_manager
    return db_manager


def get_evmars_client_instance():
    from watch_app import evmars_client
    return evmars_client


def get_eview_mqtt_service():
    from watch_app import eview_mqtt_service
    return eview_mqtt_service


async def _verify_device_access(device_id: str, user_id: str) -> None:
    """Raise 403 if user doesn't own this device."""
    owners = await get_user_manager().get_device_owners(device_id)
    if user_id not in owners:
        raise HTTPException(status_code=403, detail="Access denied to this device")


# ==================== EVIEW STATIC ENDPOINTS (must be before dynamic {device_id} routes) ====================

@router.get("/api/eview/button-events", tags=["eview"], summary="Get all button press events")
async def get_button_press_events(
    device_id: Optional[str] = Query(None, description="Filter by device ID"),
    start_date: Optional[str] = Query(None, description="Start date (ISO format)"),
    end_date: Optional[str] = Query(None, description="End date (ISO format)"),
    limit: int = Query(100, ge=1, le=1000),
    current_user: Dict = Depends(get_current_user)
):
    """Get button press events (SOS, side buttons) from all user's Eview devices."""
    try:
        # Get user's Eview devices
        user_devices = await get_user_manager().list_user_devices(current_user["user_id"], device_type='PENDANT')
        user_device_ids = {d['device_id'] for d in user_devices}

        if not user_device_ids:
            return []

        start_dt = datetime.datetime.fromisoformat(start_date) if start_date else None
        end_dt = datetime.datetime.fromisoformat(end_date) if end_date else None

        # If specific device requested, verify access
        if device_id:
            if device_id not in user_device_ids:
                raise HTTPException(status_code=403, detail="Access denied to this device")
            events = await get_eview_event_manager().get_button_press_events(
                device_id=device_id,
                start_date=start_dt,
                end_date=end_dt,
                limit=limit
            )
        else:
            # Get events from all user's devices
            all_events = []
            for dev_id in user_device_ids:
                events = await get_eview_event_manager().get_button_press_events(
                    device_id=dev_id,
                    start_date=start_dt,
                    end_date=end_dt,
                    limit=limit
                )
                all_events.extend(events)

            # Sort by timestamp and limit
            all_events.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
            events = all_events[:limit]

        return events
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {e}")
    except Exception as e:
        logger.error(f"Error getting button press events: {e}")
        raise HTTPException(status_code=500, detail="Failed to get button events")


@router.get("/api/eview/mqtt/status", tags=["eview"], summary="Get MQTT service status", response_model=EviewMQTTStatus)
async def get_mqtt_status(current_user: Dict = Depends(get_current_user)):
    """Get status of the Eview MQTT subscription service."""
    mqtt_service = get_eview_mqtt_service()
    if not mqtt_service:
        raise HTTPException(status_code=503, detail="MQTT service not initialized")

    return mqtt_service.get_status()


@router.post("/api/eview/mqtt/start", tags=["eview"], summary="Start MQTT service")
async def start_mqtt_service_endpoint(current_user: Dict = Depends(get_current_user)):
    """Start the Eview MQTT subscription service."""
    import watch_app
    from eview.mqtt_startup import start_mqtt_service

    mqtt_service = get_eview_mqtt_service()
    if mqtt_service and mqtt_service.is_running():
        return {"detail": "MQTT service already running", "status": mqtt_service.get_status()}

    watch_app.eview_mqtt_service = start_mqtt_service(
        get_eview_event_manager(), get_db_manager()
    )

    if not watch_app.eview_mqtt_service:
        raise HTTPException(status_code=500, detail="Failed to start MQTT service")

    return {"detail": "MQTT service started", "status": watch_app.eview_mqtt_service.get_status()}


@router.post("/api/eview/mqtt/stop", tags=["eview"], summary="Stop MQTT service")
async def stop_mqtt_service(current_user: Dict = Depends(get_current_user)):
    """Stop the Eview MQTT subscription service."""
    mqtt_service = get_eview_mqtt_service()
    if not mqtt_service:
        raise HTTPException(status_code=503, detail="MQTT service not initialized")

    mqtt_service.stop()
    return {"detail": "MQTT service stopped"}


# ==================== DEVICE MANAGEMENT ENDPOINTS ====================

@router.get("/api/user/devices", tags=["devices"], summary="List all devices linked to user")
async def list_user_devices(
    device_type: Optional[str] = Query(None, description="Filter by device type: 'PENDANT' or 'HUB'"),
    current_user: Dict = Depends(get_current_user)
):
    """Get all devices (Eview buttons) linked to the authenticated user."""
    try:
        records = await get_user_manager().list_user_devices(current_user["user_id"], device_type=device_type)
        return records
    except Exception as e:
        logger.error(f"Error listing user devices: {e}")
        raise HTTPException(status_code=500, detail="Failed to list devices")


@router.post("/api/user/devices/link", tags=["devices"], summary="Link a device to user", response_model=DeviceAssociation)
async def link_device(body: LinkDeviceRequest, current_user: Dict = Depends(get_current_user)):
    """Link a device (Eview button) to the authenticated user."""
    try:
        record = await get_user_manager().link_device_to_user(
            user_id=current_user["user_id"],
            device_id=body.device_id,
            device_type=body.device_type,
            label=body.label,
            product_id=body.product_id
        )

        # If Eview button, add to MQTT monitoring
        mqtt_service = get_eview_mqtt_service()
        if body.device_type == 'PENDANT' and mqtt_service:
            mqtt_service.add_device(body.device_id)

        return record
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to link device for user {current_user['user_id']}: {e}")
        raise HTTPException(status_code=500, detail="Failed to link device")


@router.delete("/api/user/devices/{device_id}", tags=["devices"], summary="Unlink a device from user")
async def unlink_device(device_id: str, current_user: Dict = Depends(get_current_user)):
    """Unlink a device from the authenticated user."""
    try:
        removed = await get_user_manager().unlink_device_from_user(
            user_id=current_user["user_id"],
            device_id=device_id
        )

        if not removed:
            raise HTTPException(status_code=404, detail="Device not linked to this user")

        # Remove from MQTT monitoring
        mqtt_service = get_eview_mqtt_service()
        if mqtt_service:
            mqtt_service.remove_device(device_id)

        return {"detail": "Device unlinked successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error unlinking device {device_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to unlink device")


def _merge_live_online(status: Dict, device_id: str) -> Dict:
    """
    The DB-backed get_device_status only persists ALARM events, so between
    alarms a live device looks "offline" even though it is heart-beating every
    minute. Merge in the MQTT service's per-device last-seen (which is updated
    by every event including trackerRealTime) so the flag tracks reality.
    """
    import time as _time
    mqtt_svc = get_eview_mqtt_service()
    if not mqtt_svc:
        return status
    last_seen_epoch = mqtt_svc.get_device_last_seen(device_id)
    if last_seen_epoch is None:
        return status
    age_min = (_time.time() - last_seen_epoch) / 60.0
    if age_min < 10:
        status["online"] = True
        status["last_seen_source"] = "mqtt_live"
    return status


@router.get("/api/eview/{device_id}/status", tags=["eview"], summary="Get Eview device status", response_model=EviewStatus)
async def get_eview_status(device_id: str, current_user: Dict = Depends(get_current_user)):
    """Get current status of an Eview button device."""
    try:
        # Verify user has access to this device
        await _verify_device_access(device_id, current_user["user_id"])

        status = await get_eview_event_manager().get_device_status(device_id)
        if not status:
            # Even with no persisted events, a live heartbeat means the device is on.
            import time as _time
            mqtt_svc = get_eview_mqtt_service()
            last_seen_epoch = mqtt_svc.get_device_last_seen(device_id) if mqtt_svc else None
            if last_seen_epoch is not None and (_time.time() - last_seen_epoch) / 60.0 < 10:
                return EviewStatus(device_id=device_id, online=True)
            return EviewStatus(device_id=device_id)

        return _merge_live_online(status, device_id)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting Eview status for {device_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get device status")


@router.get("/api/eview/{device_id}/events", tags=["eview"], summary="Get Eview device events")
async def get_eview_events(
    device_id: str,
    start_date: Optional[str] = Query(None, description="Start date (ISO format)"),
    end_date: Optional[str] = Query(None, description="End date (ISO format)"),
    event_type: Optional[str] = Query(None, description="Filter by event type"),
    limit: int = Query(100, ge=1, le=1000),
    current_user: Dict = Depends(get_current_user)
):
    """Get events for an Eview button device."""
    try:
        # Verify user has access to this device
        await _verify_device_access(device_id, current_user["user_id"])

        start_dt = datetime.datetime.fromisoformat(start_date) if start_date else None
        end_dt = datetime.datetime.fromisoformat(end_date) if end_date else None

        events = await get_eview_event_manager().get_events_by_device(
            device_id=device_id,
            start_date=start_dt,
            end_date=end_dt,
            event_type=event_type,
            limit=limit
        )
        return events
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid date format: {e}")
    except Exception as e:
        logger.error(f"Error getting Eview events for {device_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get device events")


@router.get("/api/eview/{device_id}/location", tags=["eview"], summary="Get Eview device location")
async def get_eview_location(device_id: str, current_user: Dict = Depends(get_current_user)):
    """Get latest location of an Eview button device."""
    try:
        # Verify user has access to this device
        await _verify_device_access(device_id, current_user["user_id"])

        status = await get_eview_event_manager().get_device_status(device_id)
        if not status or not status.get('latitude') or not status.get('longitude'):
            raise HTTPException(status_code=404, detail="No location data available")

        return {
            "device_id": device_id,
            "latitude": status.get('latitude'),
            "longitude": status.get('longitude'),
            "accuracy_meters": status.get('accuracy_meters'),
            "is_gps": status.get('is_gps'),
            "is_wifi": status.get('is_wifi'),
            "is_gsm": status.get('is_gsm'),
            "last_seen": status.get('last_seen'),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting Eview location for {device_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get device location")


@router.get("/api/eview/{device_id}/realtime", tags=["eview"], summary="Fetch real-time status from EVMars")
async def get_eview_realtime(device_id: str, current_user: Dict = Depends(get_current_user)):
    """
    Fetch real-time device status directly from EVMars cloud API.
    Use this to get current status when MQTT hasn't received recent updates.
    """
    try:
        await _verify_device_access(device_id, current_user["user_id"])

        # Fetch from EVMars API via singleton client
        evmars_data = get_evmars_client_instance().get_device_realtime(device_id)

        if not evmars_data:
            raise HTTPException(status_code=404, detail="Could not fetch device data from EVMars")

        # Parse the EVMars response
        # Structure: {'message': 'success', 'result': {'generalData': {...}, 'latestLocation': {...}}}
        mqtt_status = await get_eview_event_manager().get_device_status(device_id)
        is_online = bool(mqtt_status.get("online", False)) if mqtt_status else False

        # Fold in live heartbeats from the MQTT listener (trackerRealTime is
        # not persisted to EviewEvent, so the DB-only check shows false-offline
        # for a device that is actively heart-beating between alarms).
        if not is_online:
            import time as _time
            mqtt_svc = get_eview_mqtt_service()
            last_seen_epoch = mqtt_svc.get_device_last_seen(device_id) if mqtt_svc else None
            if last_seen_epoch is not None and (_time.time() - last_seen_epoch) / 60.0 < 10:
                is_online = True

        result = evmars_data.get('result', {})
        general_data = result.get('generalData', {})
        location_data = result.get('latestLocation', {})

        return {
            "device_id": device_id,
            "device_name": None,
            "online": is_online,
            "battery": general_data.get('battery'),
            "signal": general_data.get('signalSize'),
            "latitude": location_data.get('lat'),
            "longitude": location_data.get('lng'),
            "accuracy_meters": location_data.get('radius'),
            "is_gps": general_data.get('isGPS'),
            "is_wifi": general_data.get('isWIFI'),
            "is_gsm": general_data.get('isGSM'),
            "is_charging": general_data.get('isCharging'),
            "is_motion": general_data.get('isMotion'),
            "work_mode": general_data.get('workMode'),
            "timestamp": location_data.get('dateTime'),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching EVMars realtime for {device_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch device status")
