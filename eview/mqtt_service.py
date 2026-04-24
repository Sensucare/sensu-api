"""
Eview MQTT Subscriber Service
Subscribes to EVMars MQTT broker for real-time device events from Eview personal alarm buttons.
"""

import json
import time
import logging
import threading
import os
from typing import Dict, List, Optional, Callable, Any, Set
import paho.mqtt.client as mqtt

from eview.alarm_parser import parse_alarm_code, is_fall_detection, is_battery_low, is_geofence_alert

logger = logging.getLogger(__name__)


class EviewMQTTService:
    """MQTT subscriber service for Eview device events."""

    # Button types based on alarm code bits
    BUTTON_TYPES = {
        12: "SOS Button",
        13: "Side Call Button 1",
        14: "Side Call Button 2",
        17: "SOS Ending",
        11: "SOS Stop"
    }

    # String-based alarm types from simplified payload format
    # Maps alarmType string -> button type name (same names as BUTTON_TYPES values)
    ALARM_TYPE_MAP = {
        "sosKey": "SOS Button",
        "sosEnd": "SOS Ending",
        "sosStop": "SOS Stop",
        "sideKey1": "Side Call Button 1",
        "sideKey2": "Side Call Button 2",
        "fallDown": "Fall Detection",
        "batteryLow": "Battery Low",
    }

    # Geofence alarm types: geo1-geo4 map to zone numbers 1-4
    GEOFENCE_ALARM_TYPES = {
        "geo1": 1,
        "geo2": 2,
        "geo3": 3,
        "geo4": 4,
    }

    def __init__(self,
                 mqtt_host: Optional[str] = None,
                 mqtt_port: Optional[int] = None,
                 username: Optional[str] = None,
                 password: Optional[str] = None,
                 client_id: Optional[str] = None,
                 product_id: Optional[str] = None,
                 on_event_callback: Optional[Callable[[str, str, Dict], None]] = None,
                 on_button_press_callback: Optional[Callable[[str, str, Dict], None]] = None,
                 on_fall_detection_callback: Optional[Callable[[str, Dict], None]] = None,
                 on_geofence_alert_callback: Optional[Callable[[str, Dict, Dict], None]] = None,
                 on_battery_low_callback: Optional[Callable[[str, Dict], None]] = None):
        """
        Initialize MQTT subscriber service.

        Args:
            mqtt_host: MQTT broker hostname (env: EVIEW_MQTT_HOST)
            mqtt_port: MQTT broker port (env: EVIEW_MQTT_PORT)
            username: MQTT username (env: EVIEW_MQTT_USERNAME)
            password: MQTT password (env: EVIEW_MQTT_PASSWORD)
            client_id: MQTT client ID (env: EVIEW_MQTT_CLIENT_ID)
            product_id: Eview product ID (env: EVIEW_PRODUCT_ID)
            on_event_callback: Called for all events (device_id, event_type, data)
            on_button_press_callback: Called for button press events (device_id, button_type, data)
            on_fall_detection_callback: Called on fall detection (device_id, event_data)
            on_geofence_alert_callback: Called on geofence entry/exit (device_id, alarm_info, event_data)
            on_battery_low_callback: Called on battery low (device_id, event_data)
        """
        # Load from environment variables with defaults
        self.mqtt_host = mqtt_host or os.getenv('EVIEW_MQTT_HOST', 'test-loctube-mq.katchu.cn')
        self.mqtt_port = mqtt_port or int(os.getenv('EVIEW_MQTT_PORT', '38005'))
        self.username = username or os.getenv('EVIEW_MQTT_USERNAME', 'Dj4RsEe2xk8YGpTb')
        self.password = password or os.getenv('EVIEW_MQTT_PASSWORD', 'jk5xSAPHQPtTWD2yaZpQGxH7')
        self.client_id = client_id or os.getenv('EVIEW_MQTT_CLIENT_ID', 'Dj4RsEe2xk8YGpTb||sensu')
        self.product_id = product_id or os.getenv('EVIEW_PRODUCT_ID', 'fae')

        # Callbacks
        self.on_event_callback = on_event_callback
        self.on_button_press_callback = on_button_press_callback
        self.on_fall_detection_callback = on_fall_detection_callback
        self.on_geofence_alert_callback = on_geofence_alert_callback
        self.on_battery_low_callback = on_battery_low_callback

        # Device tracking
        self._monitored_devices: Set[str] = set()
        self._devices_lock = threading.Lock()

        # Per-device battery thresholds (device_id -> percentage).
        # Used for software-side battery-low detection independent of the
        # hardware alarm bit.  Updated from DB at startup and when users
        # change settings via the API.
        self._battery_thresholds: Dict[str, int] = {}
        self._thresholds_lock = threading.Lock()

        # Cooldown tracking for software battery-low alerts.
        # Maps device_id -> timestamp of last software battery_low fire.
        self._battery_low_last_fired: Dict[str, float] = {}
        self._battery_low_cooldown = 300  # 5 minutes between repeated alerts

        # MQTT client state
        self.client: Optional[mqtt.Client] = None
        self.connected = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._reconnect_delay = 5  # seconds

        # Event counters and health tracking
        self._stats_lock = threading.Lock()
        self._events_received = 0
        self._events_saved = 0
        self._events_failed = 0
        self._events_skipped_unmonitored = 0
        self._events_deduped = 0
        self._last_event_time: Optional[float] = None
        self._last_event_device: Optional[str] = None
        self._stats_last_reset = time.time()
        self._health_interval = 300  # 5 minutes
        # Per-device last-seen (monotonic seconds since epoch).
        # Updated on EVERY MQTT message including high-frequency trackerRealTime
        # heartbeats — used by the /status and /realtime routes to tell whether
        # a device is actually online, because only alarms get persisted to the
        # EviewEvent table and the DB-only check shows false-offline between alarms.
        self._last_seen_lock = threading.Lock()
        self._last_seen_per_device: Dict[str, float] = {}

    def add_device(self, device_id: str) -> None:
        """Add a device to monitor."""
        with self._devices_lock:
            if device_id not in self._monitored_devices:
                self._monitored_devices.add(device_id)
                logger.info(f"Added device {device_id} to monitoring list")

                # Subscribe to device topics if connected
                if self.connected and self.client:
                    self._subscribe_to_device(device_id)

    def remove_device(self, device_id: str) -> None:
        """Remove a device from monitoring and clean up associated caches."""
        with self._devices_lock:
            self._monitored_devices.discard(device_id)
        with self._thresholds_lock:
            self._battery_thresholds.pop(device_id, None)
            self._battery_low_last_fired.pop(device_id, None)
        logger.info(f"Removed device {device_id} from monitoring list")

    def get_monitored_devices(self) -> List[str]:
        """Get list of currently monitored devices."""
        with self._devices_lock:
            return list(self._monitored_devices)

    # ── Battery threshold cache ──────────────────────────────────────────────

    def set_battery_threshold(self, device_id: str, threshold: int) -> None:
        """Set / update the software battery-low threshold for a device.

        Clamps the value to 5-50 % to match the API-level validation.
        """
        clamped = max(5, min(50, threshold))
        if clamped != threshold:
            logger.warning(f"Battery threshold for {device_id} clamped from {threshold} to {clamped}")
        with self._thresholds_lock:
            self._battery_thresholds[device_id] = clamped
            logger.info(f"Battery threshold for {device_id} set to {clamped}%")

    def get_battery_threshold(self, device_id: str) -> int:
        """Return the configured threshold, defaulting to 20 %."""
        with self._thresholds_lock:
            return self._battery_thresholds.get(device_id, 20)

    def _should_fire_battery_low(self, device_id: str, battery: int) -> bool:
        """Check if a software battery-low alert should fire.

        Returns True only when the battery is at or below the threshold AND
        the cooldown period (5 min) has elapsed since the last alert for this device.
        Thread-safe: uses _thresholds_lock to protect the read-then-write on the
        cooldown dict.
        """
        with self._thresholds_lock:
            threshold = self._battery_thresholds.get(device_id, 20)
            if battery > threshold:
                return False

            now = time.time()
            last_fired = self._battery_low_last_fired.get(device_id, 0)
            if now - last_fired < self._battery_low_cooldown:
                return False

            self._battery_low_last_fired[device_id] = now
        return True

    def increment_saved(self) -> None:
        """Increment saved event counter (called from callbacks)."""
        with self._stats_lock:
            self._events_saved += 1

    def increment_failed(self) -> None:
        """Increment failed event counter (called from callbacks)."""
        with self._stats_lock:
            self._events_failed += 1

    def _log_health_status(self) -> None:
        """Log periodic health status with event counters."""
        with self._stats_lock:
            elapsed = time.time() - self._stats_last_reset
            elapsed_min = max(1, elapsed / 60)
            last_event_ago = (
                f"{int(time.time() - self._last_event_time)}s ago"
                if self._last_event_time else "never"
            )
            logger.info(
                f"MQTT HEALTH | connected={self.connected} | "
                f"devices_monitored={len(self._monitored_devices)} | "
                f"events_received={self._events_received} | "
                f"events_saved={self._events_saved} | "
                f"events_failed={self._events_failed} | "
                f"events_skipped_unmonitored={self._events_skipped_unmonitored} | "
                f"events_deduped={self._events_deduped} | "
                f"last_event={last_event_ago} "
                f"(device={self._last_event_device}) | "
                f"rate={self._events_received / elapsed_min:.1f}/min | "
                f"period={elapsed_min:.0f}min"
            )
            # Reset counters
            self._events_received = 0
            self._events_saved = 0
            self._events_failed = 0
            self._events_skipped_unmonitored = 0
            self._events_deduped = 0
            self._stats_last_reset = time.time()

    def _subscribe_to_device(self, device_id: str) -> None:
        """
        Subscribe to every MQTT message this device publishes.

        We use a multi-level wildcard (#) rather than enumerating specific
        event names because the device emits several message types the app
        cares about — alarms, trackerRealTime, and heartbeat/property reports
        on separate subtrees. Missing heartbeats was the cause of the online
        badge flapping: the app only ever saw online while a GPS fix was
        arriving.

        The on_message handler parses the topic and handles unknown event
        types gracefully, so this is safe to broaden.
        """
        if not self.client:
            return

        topic = f"/device/{self.product_id}/{device_id}/message/#"
        self.client.subscribe(topic, qos=1)
        logger.debug(f"Subscribed to device topic: {topic}")

    def _subscribe_to_all_devices(self) -> None:
        """Subscribe to topics for all monitored devices."""
        with self._devices_lock:
            for device_id in self._monitored_devices:
                self._subscribe_to_device(device_id)

        # Broad wildcards so we catch any device's messages including heartbeats.
        if self.client:
            wildcard_topics = [
                f"/device/{self.product_id}/+/message/#",
                "/device/+/+/message/#",
            ]
            for topic in wildcard_topics:
                self.client.subscribe(topic, qos=1)
                logger.info(f"Subscribed to wildcard topic: {topic}")

    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: Dict, rc: int) -> None:
        """Callback when MQTT client connects."""
        if rc == 0:
            self.connected = True
            logger.info(f"Connected to MQTT broker: {self.mqtt_host}:{self.mqtt_port}")
            self._subscribe_to_all_devices()
        else:
            self.connected = False
            error_messages = {
                1: "Incorrect protocol version",
                2: "Invalid client identifier",
                3: "Server unavailable",
                4: "Bad username or password",
                5: "Not authorized"
            }
            error_msg = error_messages.get(rc, f"Unknown error code: {rc}")
            logger.error(f"Failed to connect to MQTT broker: {error_msg}")

    def _on_disconnect(self, client: mqtt.Client, userdata: Any, rc: int) -> None:
        """Callback when MQTT client disconnects."""
        self.connected = False
        if rc != 0:
            logger.warning(f"Unexpected disconnection from MQTT broker (rc={rc}). Will attempt reconnect.")
        else:
            logger.info("Disconnected from MQTT broker")

    def _on_message(self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
        """Callback when a message is received."""
        try:
            topic = msg.topic
            payload = msg.payload.decode('utf-8')

            logger.debug(f"Received message on topic: {topic}")

            with self._stats_lock:
                self._events_received += 1
                self._last_event_time = time.time()

            # Parse JSON payload
            data = json.loads(payload)

            # Process the event
            self._process_event(topic, data)

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON payload on topic {msg.topic}: {e} | raw={msg.payload[:200]}")
            with self._stats_lock:
                self._events_failed += 1
        except Exception as e:
            logger.error(f"Error processing MQTT message on topic {msg.topic}: {e}", exc_info=True)
            with self._stats_lock:
                self._events_failed += 1

    def _extract_device_info(self, topic: str) -> Dict[str, Optional[str]]:
        """Extract device ID and event type from MQTT topic."""
        # Topic formats:
        # /device/{productId}/{deviceId}/message/event/{eventType}
        # /tenant/{tenantId}/device/{productId}/{deviceId}/message/event/{eventType}
        parts = topic.split('/')

        if 'device' in parts:
            device_idx = parts.index('device')
            return {
                'product_id': parts[device_idx + 1] if len(parts) > device_idx + 1 and parts[device_idx + 1] != '*' else None,
                'device_id': parts[device_idx + 2] if len(parts) > device_idx + 2 and parts[device_idx + 2] != '*' else None,
                'event_type': parts[-1] if len(parts) > 4 else None
            }
        return {'product_id': None, 'device_id': None, 'event_type': None}

    def _parse_button_type(self, alarm_code: Optional[int]) -> Optional[str]:
        """Parse alarm code to identify button type."""
        if alarm_code is None:
            return None

        for bit, button_name in self.BUTTON_TYPES.items():
            if alarm_code & (1 << bit):
                return button_name
        return None

    def _process_event(self, topic: str, data: Dict) -> None:
        """Process incoming MQTT event."""
        # Extract device info from topic or payload
        topic_info = self._extract_device_info(topic)
        device_id = topic_info.get('device_id') or data.get('deviceId') or 'unknown'
        event_type = topic_info.get('event_type') or data.get('event') or 'unknown'

        # Check if this is a monitored device (if we have any in the list)
        with self._devices_lock:
            if self._monitored_devices and device_id not in self._monitored_devices:
                logger.info(f"Ignoring event from unmonitored device: {device_id} (event_type={event_type})")
                with self._stats_lock:
                    self._events_skipped_unmonitored += 1
                return

        # Extract nested data — supports both full and simplified payload formats
        nested_data = data.get('data', {})
        general_data = nested_data.get('generalData', {})
        location_data = nested_data.get('latestLocation', {})
        headers = data.get('headers', {})

        device_name = headers.get('deviceName', 'Unknown')
        battery = general_data.get('battery')

        # Location: try latestLocation first, fall back to data-level lat/lng
        # (simplified alarm payloads put lat/lng directly in data)
        lat = location_data.get('lat') or nested_data.get('lat')
        lng = location_data.get('lng') or nested_data.get('lng')

        # String-based alarm type from simplified payload format
        alarm_type_str = nested_data.get('alarmType')

        with self._stats_lock:
            self._last_event_device = device_id

        # Record per-device last-seen for every event (including heartbeats).
        with self._last_seen_lock:
            self._last_seen_per_device[device_id] = time.time()

        logger.info(
            f"Event: {event_type} | Device: {device_name} ({device_id}) | "
            f"Battery: {battery}% | alarmType: {alarm_type_str}"
        )

        if lat and lng:
            logger.info(f"  Location: {lat}, {lng}")

        # Build event data payload for callbacks
        event_data = {
            "device_id": device_id,
            "device_name": device_name,
            "event_type": event_type,
            "battery": battery,
            "latitude": lat,
            "longitude": lng,
            "accuracy_meters": location_data.get('radius'),
            "is_gps": general_data.get('isGPS'),
            "is_wifi": general_data.get('isWIFI'),
            "is_motion": general_data.get('isMotion'),
            "signal_strength": general_data.get('signalSize'),
            "timestamp": data.get('timestamp'),
            "raw_payload": data,
        }

        # Track whether a specific callback handled this event to avoid
        # duplicate saves from the generic on_event_callback.
        handled_by_specific = False

        # --- String-based alarmType detection (simplified payload format) ---
        # Some trackerAlarm payloads use a string `alarmType` field instead of
        # integer alarm code bitmasks. Handle these first.
        if event_type == 'trackerAlarm' and alarm_type_str:
            mapped_type = self.ALARM_TYPE_MAP.get(alarm_type_str)
            if mapped_type:
                logger.warning(f"  ALARM (string): {alarm_type_str} -> {mapped_type}")

                if mapped_type in ("SOS Button", "SOS Ending", "SOS Stop",
                                   "Side Call Button 1", "Side Call Button 2"):
                    # Button press
                    if self.on_button_press_callback:
                        try:
                            self.on_button_press_callback(device_id, mapped_type, data)
                            handled_by_specific = True
                        except Exception as e:
                            logger.error(f"Error in button press callback: {e}", exc_info=True)

                elif mapped_type == "Fall Detection":
                    if self.on_fall_detection_callback:
                        try:
                            self.on_fall_detection_callback(device_id, event_data)
                            handled_by_specific = True
                        except Exception as e:
                            logger.error(f"Error in fall detection callback: {e}", exc_info=True)

                elif mapped_type == "Battery Low":
                    if self.on_battery_low_callback:
                        try:
                            self.on_battery_low_callback(device_id, event_data)
                            handled_by_specific = True
                        except Exception as e:
                            logger.error(f"Error in battery low callback: {e}", exc_info=True)
            # Check for geofence alarm types (geo1-geo4)
            geo_zone = self.GEOFENCE_ALARM_TYPES.get(alarm_type_str)
            if not mapped_type and geo_zone:
                logger.warning(f"  GEOFENCE ALARM (string): {alarm_type_str} -> zone {geo_zone}")
                alarm_info = {
                    "zone_number": geo_zone,
                    "direction": "exit",  # Device sends SMS on exit; direction determined by config
                    "alarm_type": alarm_type_str,
                }
                if self.on_geofence_alert_callback:
                    try:
                        self.on_geofence_alert_callback(device_id, alarm_info, event_data)
                        handled_by_specific = True
                    except Exception as e:
                        logger.error(f"Error in geofence alert callback: {e}", exc_info=True)
            elif not mapped_type:
                logger.warning(f"  Unknown alarmType string: {alarm_type_str}")

        # --- Integer alarm code bitmask detection (legacy payload format) ---
        # Check for button press ONLY on trackerAlarm events.
        # statusCode is a persistent status bitmask that retains the last alarm
        # state across heartbeats, so checking it on trackerRealTime generates
        # false SOS alerts.  Use alarmCode which is only set on actual alarms.
        alarm_code = data.get('alarmCode') or general_data.get('alarmCode', 0)

        if event_type == 'trackerAlarm' and alarm_code and not handled_by_specific:
            button_type = self._parse_button_type(alarm_code)
            if button_type:
                logger.warning(f"  BUTTON PRESSED (bitmask): {button_type}")
                if self.on_button_press_callback:
                    try:
                        self.on_button_press_callback(device_id, button_type, data)
                        handled_by_specific = True
                    except Exception as e:
                        logger.error(f"Error in button press callback: {e}", exc_info=True)

        # Parse alarm code for fall detection, geofence, battery low (hardware bit)
        if alarm_code and not handled_by_specific:
            if self._process_alarm_code(device_id, alarm_code, event_data):
                handled_by_specific = True

        # Software battery-low check: compare reported battery level against the
        # user-configured threshold stored in the DB (cached in memory).
        # Only fires if no alarm callback already handled battery low above.
        hardware_battery_fired = (
            (alarm_code and is_battery_low(alarm_code))
            or alarm_type_str == 'batteryLow'
        )
        if (
            battery is not None
            and self.on_battery_low_callback
            and not hardware_battery_fired
            and self._should_fire_battery_low(device_id, battery)
        ):
            logger.warning(
                f"  SOFTWARE BATTERY LOW for device {device_id}: "
                f"{battery}% <= {self.get_battery_threshold(device_id)}% threshold"
            )
            try:
                self.on_battery_low_callback(device_id, event_data)
                handled_by_specific = True
            except Exception as e:
                logger.error(f"Error in software battery low callback: {e}", exc_info=True)

        # Call general event callback only if no specific callback already saved this event
        if self.on_event_callback and not handled_by_specific:
            try:
                self.on_event_callback(device_id, event_type, data)
            except Exception as e:
                logger.error(f"Error in event callback: {e}", exc_info=True)

    def _process_alarm_code(self, device_id: str, alarm_code: int, event_data: Dict) -> bool:
        """Process alarm code bits for fall detection, geofence, and battery low alerts.

        Returns True if at least one specific alarm callback was invoked.
        """
        handled = False

        # Fall detection (bit 2)
        if is_fall_detection(alarm_code):
            logger.critical(f"  FALL DETECTED for device {device_id}!")
            if self.on_fall_detection_callback:
                try:
                    self.on_fall_detection_callback(device_id, event_data)
                    handled = True
                except Exception as e:
                    logger.error(f"Error in fall detection callback: {e}", exc_info=True)

        # Battery low (bit 0)
        if is_battery_low(alarm_code):
            logger.warning(f"  BATTERY LOW for device {device_id}! Level: {event_data.get('battery')}%")
            if self.on_battery_low_callback:
                try:
                    self.on_battery_low_callback(device_id, event_data)
                    handled = True
                    # Record the fire so the software path won't duplicate
                    # within the same cooldown window.
                    with self._thresholds_lock:
                        self._battery_low_last_fired[device_id] = time.time()
                except Exception as e:
                    logger.error(f"Error in battery low callback: {e}", exc_info=True)

        # Geofence alerts (bits 4-7)
        is_geo, zone_number, direction = is_geofence_alert(alarm_code)
        if is_geo:
            logger.warning(f"  GEOFENCE {direction.upper()} zone {zone_number} for device {device_id}!")
            alarm_info = {
                "zone_number": zone_number,
                "direction": direction,
                "alarm_code": alarm_code,
            }
            if self.on_geofence_alert_callback:
                try:
                    self.on_geofence_alert_callback(device_id, alarm_info, event_data)
                    handled = True
                except Exception as e:
                    logger.error(f"Error in geofence alert callback: {e}", exc_info=True)

        return handled

    def connect(self) -> bool:
        """Connect to MQTT broker."""
        try:
            self.client = mqtt.Client(client_id=self.client_id, protocol=mqtt.MQTTv311)
            self.client.username_pw_set(self.username, self.password)

            # Set callbacks
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.on_message = self._on_message

            logger.info(f"Connecting to MQTT broker: {self.mqtt_host}:{self.mqtt_port}")
            self.client.connect(self.mqtt_host, self.mqtt_port, keepalive=60)

            # Start network loop
            self.client.loop_start()

            # Wait for connection
            timeout = 10
            start_time = time.time()
            while not self.connected and (time.time() - start_time) < timeout:
                time.sleep(0.1)

            if not self.connected:
                logger.error("Failed to connect to MQTT broker within timeout")
                return False

            return True

        except Exception as e:
            logger.error(f"Error connecting to MQTT broker: {e}", exc_info=True)
            return False

    def disconnect(self) -> None:
        """Disconnect from MQTT broker."""
        self._stop_event.set()

        if self.client:
            self.client.loop_stop()
            self.client.disconnect()
            logger.info("Disconnected from MQTT broker")

        self.connected = False

    def start_background(self) -> None:
        """Start MQTT service in background thread with auto-reconnect."""
        if self._thread and self._thread.is_alive():
            logger.warning("MQTT service already running in background")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._background_loop, daemon=True)
        self._thread.start()
        logger.info("Started MQTT service in background")

    def _background_loop(self) -> None:
        """Background loop with auto-reconnect and periodic health logging."""
        last_health_log = time.time()

        while not self._stop_event.is_set():
            if not self.connected:
                logger.info("Attempting to connect to MQTT broker...")
                if self.connect():
                    logger.info("MQTT connection established")
                else:
                    logger.warning(f"MQTT connection failed, retrying in {self._reconnect_delay}s")
                    self._stop_event.wait(self._reconnect_delay)
                    continue

            # Periodic health log
            now = time.time()
            if now - last_health_log >= self._health_interval:
                self._log_health_status()
                last_health_log = now

            # Check connection periodically
            self._stop_event.wait(1)

        logger.info("MQTT background loop stopped")

    def stop(self) -> None:
        """Stop the MQTT service."""
        self._stop_event.set()
        self.disconnect()

        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

        logger.info("MQTT service stopped")

    def is_running(self) -> bool:
        """Check if MQTT service is running."""
        return self.connected and self._thread is not None and self._thread.is_alive()

    def get_status(self) -> Dict[str, Any]:
        """Get current service status."""
        with self._stats_lock:
            last_event_ago = (
                f"{int(time.time() - self._last_event_time)}s ago"
                if self._last_event_time else "never"
            )
            return {
                "connected": self.connected,
                "running": self.is_running(),
                "broker": f"{self.mqtt_host}:{self.mqtt_port}",
                "client_id": self.client_id,
                "product_id": self.product_id,
                "monitored_devices": self.get_monitored_devices(),
                "monitored_device_count": len(self._monitored_devices),
                "events_received": self._events_received,
                "events_saved": self._events_saved,
                "events_failed": self._events_failed,
                "last_event": last_event_ago,
                "last_event_device": self._last_event_device,
            }

    def get_device_last_seen(self, device_id: str) -> Optional[float]:
        """
        Return the wall-clock time (seconds since epoch) at which we last
        received any MQTT message from this device, or None if we have not
        seen it since the service started. Updated on every event including
        trackerRealTime heartbeats that are not persisted to the DB.
        """
        with self._last_seen_lock:
            return self._last_seen_per_device.get(device_id)


# Singleton instance for use across the application
_mqtt_service: Optional[EviewMQTTService] = None


def get_mqtt_service() -> Optional[EviewMQTTService]:
    """Return the singleton MQTT service instance, or None if not yet initialized."""
    return _mqtt_service


def init_mqtt_service(
    on_event_callback: Optional[Callable[[str, str, Dict], None]] = None,
    on_button_press_callback: Optional[Callable[[str, str, Dict], None]] = None,
    on_fall_detection_callback: Optional[Callable[[str, Dict], None]] = None,
    on_geofence_alert_callback: Optional[Callable[[str, Dict, Dict], None]] = None,
    on_battery_low_callback: Optional[Callable[[str, Dict], None]] = None,
    **kwargs
) -> EviewMQTTService:
    """Initialize and start the MQTT service."""
    global _mqtt_service

    if _mqtt_service is not None:
        _mqtt_service.stop()

    _mqtt_service = EviewMQTTService(
        on_event_callback=on_event_callback,
        on_button_press_callback=on_button_press_callback,
        on_fall_detection_callback=on_fall_detection_callback,
        on_geofence_alert_callback=on_geofence_alert_callback,
        on_battery_low_callback=on_battery_low_callback,
        **kwargs
    )

    return _mqtt_service
