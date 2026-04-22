"""
MQTT service initialization and startup logic for Eview devices.
Handles event callbacks, device loading, and background service start.
"""
import asyncio
import datetime
import json
import logging
from typing import Dict, Optional

import requests

from eview.mqtt_service import EviewMQTTService, init_mqtt_service

logger = logging.getLogger(__name__)


def create_mqtt_event_handlers(eview_event_manager, loop: asyncio.AbstractEventLoop, mqtt_service_ref=None):
    """
    Create MQTT event callback functions bound to the given event manager.

    Since MQTT callbacks run in a separate thread, we use asyncio.run_coroutine_threadsafe
    to schedule async database operations on the main event loop.

    Args:
        mqtt_service_ref: Optional list containing [mqtt_service] for counter updates.
                          Using a list so it can be set after creation.

    Returns a dict of callbacks suitable for passing to init_mqtt_service().
    """
    _mqtt_ref = mqtt_service_ref or [None]

    def _increment_saved():
        if _mqtt_ref[0]:
            _mqtt_ref[0].increment_saved()

    def _increment_failed():
        if _mqtt_ref[0]:
            _mqtt_ref[0].increment_failed()

    def _run_async(coro, context: str = "unknown"):
        """Helper to run async code from MQTT thread with retry."""
        max_retries = 2
        for attempt in range(1, max_retries + 1):
            try:
                future = asyncio.run_coroutine_threadsafe(coro, loop)
                future.result(timeout=15)
                return True
            except asyncio.TimeoutError:
                logger.error(
                    f"Async operation timed out (attempt {attempt}/{max_retries}, context={context})"
                )
            except Exception as e:
                logger.error(
                    f"Async operation failed (attempt {attempt}/{max_retries}, context={context}): {e}",
                    exc_info=True
                )
            if attempt < max_retries:
                import time
                time.sleep(1)
        logger.error(f"Async operation exhausted all retries (context={context})")
        _increment_failed()
        return False

    EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"

    ALERT_NOTIFICATIONS = {
        "sos": {"title": "Alerta SOS", "body": "Se activó la alerta SOS en el dispositivo"},
        "fall_detection": {"title": "Caída detectada", "body": "Se detectó una posible caída"},
        "battery_low": {"title": "Batería baja", "body": "La batería del dispositivo está baja ({battery}%)"},
        "geofence_exit": {"title": "Geocerca", "body": "El dispositivo salió de la zona segura"},
        "geofence_enter": {"title": "Geocerca", "body": "El dispositivo entró a la zona"},
    }

    async def _get_push_tokens_for_device(device_id: str):
        """Get push tokens for all users linked to a device."""
        from core.database import DatabaseManager, DATABASE_URL
        db = DatabaseManager(DATABASE_URL)
        try:
            await db.init_pool()
            rows = await db.fetch('''
                SELECT u."expoPushToken"
                FROM "User" u
                JOIN "UserDevice" ud ON u.id = ud."userId"
                WHERE ud."eviewDeviceId" = $1
                AND u."expoPushToken" IS NOT NULL
            ''', device_id)
            return [row["expoPushToken"] for row in rows]
        except Exception as e:
            logger.error(f"Failed to query push tokens for device {device_id}: {e}")
            return []
        finally:
            await db.close()

    def _send_push_notification(device_id: str, event_type: str, extra_data: Optional[Dict] = None):
        """Send push notification to all users linked to a device."""
        notif = ALERT_NOTIFICATIONS.get(event_type)
        if not notif:
            return

        async def _send():
            tokens = await _get_push_tokens_for_device(device_id)
            if not tokens:
                return

            body = notif["body"]
            if extra_data:
                try:
                    body = body.format(**extra_data)
                except (KeyError, ValueError):
                    pass

            for token in tokens:
                try:
                    resp = requests.post(EXPO_PUSH_URL, json={
                        "to": token,
                        "title": notif["title"],
                        "body": body,
                        "sound": "default",
                        "priority": "high",
                        "data": {
                            "type": event_type,
                            "device_id": device_id,
                        },
                    }, timeout=10)
                    if resp.status_code == 200:
                        logger.info(f"Push notification sent for {event_type} to {token[:30]}...")
                    else:
                        logger.warning(f"Expo Push API error: {resp.status_code}")
                except Exception as e:
                    logger.warning(f"Failed to send push notification: {e}")

        _run_async(_send(), context=f"push_notification:{event_type}:{device_id}")

    def on_eview_event(device_id: str, event_type: str, data: Dict):
        """Handle incoming Eview events.

        Only saves trackerAlarm events. trackerRealTime (periodic GPS pings) are
        skipped here — they are not user-facing alerts. Specific callbacks
        (on_button_press, on_fall_detected, etc.) still handle classified events
        from either topic independently.
        """
        if event_type == 'trackerRealTime':
            logger.info(f"Skipping trackerRealTime generic save for device {device_id}")
            return

        async def _save():
            try:
                event_id = await eview_event_manager.save_event(
                    device_id=device_id,
                    event_type=event_type,
                    timestamp=datetime.datetime.now(),
                    event_data=data
                )
                if event_id:
                    logger.info(f"Saved Eview event: {event_type} for device {device_id}")
                    _increment_saved()
                else:
                    logger.info(f"Eview event deduped: {event_type} for device {device_id}")
            except Exception as e:
                logger.error(f"Failed to save Eview event: {e} | device={device_id} | payload={json.dumps(data)[:500]}")
                _increment_failed()

        _run_async(_save(), context=f"save_event:{event_type}:{device_id}")

    def on_button_press(device_id: str, button_type: str, data: Dict):
        """Handle button press events - these are critical alerts."""
        logger.warning(f"ALERT: Button press detected! Device: {device_id}, Button: {button_type}")

        # Map button type to event type
        if button_type == "SOS Button":
            event_type = 'sos'
        elif button_type in ("SOS Ending", "SOS Stop"):
            # Don't save SOS end events as separate alerts
            return
        else:
            event_type = 'button_press'

        async def _save():
            try:
                event_id = await eview_event_manager.save_event(
                    device_id=device_id,
                    event_type=event_type,
                    timestamp=datetime.datetime.now(),
                    event_data=data
                )
                if event_id:
                    logger.info(f"Saved button press event: {event_type} for device {device_id}")
                    _increment_saved()
                else:
                    logger.info(f"Button press event deduped: {event_type} for device {device_id}")
            except Exception as e:
                logger.error(f"Failed to save button press event: {e} | device={device_id} | payload={json.dumps(data)[:500]}")
                _increment_failed()

        _run_async(_save(), context=f"button_press:{button_type}:{device_id}")

        # Send push notification for SOS alerts
        if event_type == 'sos':
            _send_push_notification(device_id, 'sos')

    def on_fall_detected(device_id: str, event_data: Dict):
        """Handle fall detection alarm from MQTT."""
        logger.critical(f"FALL DETECTED: Device {device_id}")

        async def _save():
            try:
                event_id = await eview_event_manager.save_event(
                    device_id=device_id,
                    event_type='fall_detection',
                    timestamp=datetime.datetime.now(),
                    event_data=event_data.get('raw_payload', event_data)
                )
                if event_id:
                    logger.info(f"Saved fall detection event for device {device_id}")
                    _increment_saved()
            except Exception as e:
                logger.error(f"Failed to save fall detection event: {e} | device={device_id} | payload={json.dumps(event_data)[:500]}")
                _increment_failed()

        _run_async(_save(), context=f"fall_detection:{device_id}")

        # Send push notification for fall detection
        _send_push_notification(device_id, 'fall_detection')

    def on_battery_low(device_id: str, event_data: Dict):
        """Handle battery low alarm from MQTT."""
        logger.warning(f"BATTERY LOW: Device {device_id}, Level: {event_data.get('battery')}%")

        async def _save():
            try:
                event_id = await eview_event_manager.save_event(
                    device_id=device_id,
                    event_type='battery_low',
                    timestamp=datetime.datetime.now(),
                    event_data=event_data.get('raw_payload', event_data)
                )
                if event_id:
                    logger.info(f"Saved battery low event for device {device_id}")
                    _increment_saved()
            except Exception as e:
                logger.error(f"Failed to save battery low event: {e} | device={device_id}")
                _increment_failed()

        _run_async(_save(), context=f"battery_low:{device_id}")

        # Send push notification for battery low
        battery = event_data.get('battery', event_data.get('raw_payload', {}).get('battery', '?'))
        _send_push_notification(device_id, 'battery_low', {"battery": battery})

    def on_geofence_alert(device_id: str, alarm_info: Dict, event_data: Dict):
        """Handle geofence entry/exit from MQTT."""
        direction = alarm_info.get('direction', 'unknown')
        zone_number = alarm_info.get('zone_number', 0)
        event_type = f"geofence_{direction}"
        logger.warning(f"GEOFENCE {direction.upper()}: Device {device_id}, Zone {zone_number}")

        async def _save():
            try:
                event_id = await eview_event_manager.save_event(
                    device_id=device_id,
                    event_type=event_type,
                    timestamp=datetime.datetime.now(),
                    event_data=event_data.get('raw_payload', event_data)
                )
                if event_id:
                    logger.info(f"Saved geofence event: {event_type} for device {device_id}")
                    _increment_saved()
            except Exception as e:
                logger.error(f"Failed to save geofence event: {e} | device={device_id}")
                _increment_failed()

        _run_async(_save(), context=f"geofence:{event_type}:{device_id}")

        # Send push notification for geofence alerts
        _send_push_notification(device_id, event_type)

    return {
        "on_event_callback": on_eview_event,
        "on_button_press_callback": on_button_press,
        "on_fall_detection_callback": on_fall_detected,
        "on_battery_low_callback": on_battery_low,
        "on_geofence_alert_callback": on_geofence_alert,
    }


async def start_mqtt_service(eview_event_manager, db_manager) -> Optional[EviewMQTTService]:
    """
    Initialize and start the Eview MQTT service.

    Args:
        eview_event_manager: EviewEventManager instance for persisting events.
        db_manager: DatabaseManager instance for loading linked devices.

    Returns:
        The running EviewMQTTService instance, or None on failure.
    """
    try:
        # Get the current event loop for async callbacks
        loop = asyncio.get_running_loop()

        # Use a mutable ref so callbacks can access the service after creation
        mqtt_ref = [None]
        callbacks = create_mqtt_event_handlers(eview_event_manager, loop, mqtt_service_ref=mqtt_ref)
        mqtt_service = init_mqtt_service(**callbacks)
        mqtt_ref[0] = mqtt_service

        # Load existing linked devices and their battery thresholds from database
        # Use DISTINCT to avoid duplicates when multiple users link same device
        async with db_manager.acquire() as conn:
            rows = await conn.fetch("""
                SELECT DISTINCT ud."eviewDeviceId", d."batteryThreshold"
                FROM "UserDevice" ud
                JOIN "Device" d ON ud."eviewDeviceId" = d."deviceId"
                WHERE d."deviceType" = 'PENDANT'
            """)
            for row in rows:
                device_id = row['eviewDeviceId']
                mqtt_service.add_device(device_id)
                threshold = row['batteryThreshold']
                if threshold is not None:
                    mqtt_service.set_battery_threshold(device_id, threshold)
            logger.info(f"Added {len(rows)} unique devices to MQTT monitoring")

        # Start the MQTT service in background
        mqtt_service.start_background()
        logger.info("Eview MQTT service started successfully")
        return mqtt_service

    except Exception as e:
        logger.error(f"Failed to start Eview MQTT service: {e}")
        return None
