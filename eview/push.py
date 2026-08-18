"""
Shared push-notification helpers (Juan 2026-06-25).

Hoisted out of `mqtt_startup.py` where the MQTT callback closure
defined `_send_push_notification` privately. Routes outside the
MQTT path (notably the app-side SOS endpoint added 2026-06-24)
now need the same fan-out so a press on the mobile button reaches
every paired family device, not just the operator board.

The two surfaces share:

* `ALERT_NOTIFICATIONS` — Spanish title/body strings per event type
* `get_push_tokens_for_device(device_id)` — async lookup against
  the shared sensu_pay database
* `send_push_notification(device_id, event_type, extra_data=None)`
  — async fire-and-forget POST to Expo's push API
* `send_push_notification_threadsafe(...)` — sync wrapper for the
  MQTT callback thread that schedules the coroutine on the main
  event loop

Best-effort by design: a Resend / Expo / DB hiccup logs a warning
but never raises into the caller.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"

# Title/body pairs per event_type. Strings keep the same wording the
# MQTT callbacks used pre-2026-06-25 so families do not notice a
# copy change when the path swaps.
ALERT_NOTIFICATIONS: Dict[str, Dict[str, str]] = {
    "sos": {
        "title": "Alerta SOS",
        "body": "Se activó la alerta SOS en el dispositivo",
    },
    "fall_detection": {
        "title": "Caída detectada",
        "body": "Se detectó una posible caída",
    },
    "battery_low": {
        "title": "Batería baja",
        "body": "La batería del dispositivo está baja ({battery}%)",
    },
    "geofence_exit": {
        "title": "Geocerca",
        "body": "El dispositivo salió de la zona segura",
    },
    "geofence_enter": {
        "title": "Geocerca",
        "body": "El dispositivo entró a la zona",
    },
}


async def get_push_tokens_for_device(device_id: str) -> List[str]:
    """Return every Expo push token paired to a device.

    Looks up users joined through UserDevice and returns their
    `expoPushToken` strings, filtered to non-null. Reads from the
    shared sensu_pay database via a freshly-pooled DatabaseManager
    (cheap because asyncpg's pool is global per-process).
    """
    from core.database import DatabaseManager, DATABASE_URL  # local import to avoid cycles
    db = DatabaseManager(DATABASE_URL)
    try:
        await db.init_pool()
        rows = await db.fetch(
            '''
            SELECT u."expoPushToken"
            FROM "User" u
            JOIN "UserDevice" ud ON u.id = ud."userId"
            WHERE ud."eviewDeviceId" = $1
              AND u."expoPushToken" IS NOT NULL
            ''',
            device_id,
        )
        return [row["expoPushToken"] for row in rows]
    except Exception as e:  # pragma: no cover — defensive
        logger.error(f"Failed to query push tokens for device {device_id}: {e}")
        return []
    finally:
        await db.close()


async def send_push_notification(
    device_id: str,
    event_type: str,
    extra_data: Optional[Dict[str, Any]] = None,
) -> int:
    """Fan-out an Expo push to every device paired to `device_id`.

    Returns the count of tokens we attempted to push to (regardless of
    Expo's per-token response). Returns 0 if there are no paired tokens
    or the event_type has no notification template.
    """
    notif = ALERT_NOTIFICATIONS.get(event_type)
    if not notif:
        return 0

    tokens = await get_push_tokens_for_device(device_id)
    if not tokens:
        return 0

    body = notif["body"]
    if extra_data:
        try:
            body = body.format(**extra_data)
        except (KeyError, ValueError):
            # Format-string mismatch (e.g. battery_low body wants
            # {battery} but caller didn't supply one). Fall back to
            # the raw template — sending something is better than
            # silently dropping the alert.
            pass

    for token in tokens:
        try:
            resp = requests.post(
                EXPO_PUSH_URL,
                json={
                    "to": token,
                    "title": notif["title"],
                    "body": body,
                    "sound": "default",
                    "priority": "high",
                    "data": {
                        "type": event_type,
                        "device_id": device_id,
                    },
                },
                timeout=10,
            )
            if resp.status_code == 200:
                logger.info(
                    f"Push notification sent for {event_type} to {token[:30]}..."
                )
            else:
                logger.warning(f"Expo Push API error: {resp.status_code}")
        except Exception as e:
            logger.warning(f"Failed to send push notification: {e}")

    return len(tokens)


def send_push_notification_threadsafe(
    device_id: str,
    event_type: str,
    extra_data: Optional[Dict[str, Any]] = None,
    *,
    loop: Optional[asyncio.AbstractEventLoop] = None,
    run_async: Optional[Callable[[Awaitable[Any], str], bool]] = None,
) -> None:
    """Schedule `send_push_notification` from a non-async thread.

    The MQTT callback thread calls this; if a `run_async(coro, context)`
    helper is supplied (the existing one in mqtt_startup.create_mqtt_event_handlers
    handles retries + failure counters), it is used. Otherwise the
    coroutine is scheduled on the supplied event loop with
    `asyncio.run_coroutine_threadsafe`. Both paths are fire-and-forget.
    """
    coro = send_push_notification(device_id, event_type, extra_data)
    context = f"push_notification:{event_type}:{device_id}"
    if run_async is not None:
        run_async(coro, context)
        return
    if loop is None:
        logger.warning(
            f"send_push_notification_threadsafe called without loop or run_async ({context}); skipping"
        )
        return
    try:
        asyncio.run_coroutine_threadsafe(coro, loop)
    except Exception as e:
        logger.warning(f"Failed to schedule push send ({context}): {e}")
