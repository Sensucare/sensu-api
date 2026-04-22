"""
PostgreSQL Database Manager for Sensu API

This module provides async database access to the unified Prisma-managed PostgreSQL database.
Table and column names match the Prisma schema (PascalCase tables, camelCase columns).
"""

import asyncio
import datetime
import logging
import json
import uuid
import os
from typing import Dict, List, Optional, Any
from contextlib import asynccontextmanager

import asyncpg
from asyncpg import Pool, Connection

logger = logging.getLogger(__name__)

# Database URL from environment
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://sensu:sensu_pass@localhost:5432/sensu_pay")


class DatabaseManager:
    """Async PostgreSQL database manager using asyncpg."""

    def __init__(self, database_url: Optional[str] = None):
        self.database_url = database_url or DATABASE_URL
        self._pool: Optional[Pool] = None

    async def init_pool(self) -> None:
        """Initialize the connection pool."""
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self.database_url,
                min_size=5,
                max_size=20,
                command_timeout=60
            )
            logger.info("Database connection pool initialized")

    async def close_pool(self) -> None:
        """Close the connection pool."""
        if self._pool:
            await self._pool.close()
            self._pool = None
            logger.info("Database connection pool closed")

    async def close(self) -> None:
        """Alias for close_pool()."""
        await self.close_pool()

    @asynccontextmanager
    async def acquire(self):
        """Context manager to acquire a connection from the pool."""
        if self._pool is None:
            await self.init_pool()
        async with self._pool.acquire() as conn:
            yield conn

    async def execute(self, query: str, *args) -> str:
        """Execute a query and return status."""
        async with self.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetch(self, query: str, *args) -> List[asyncpg.Record]:
        """Execute a query and return all rows."""
        async with self.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args) -> Optional[asyncpg.Record]:
        """Execute a query and return a single row."""
        async with self.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args) -> Any:
        """Execute a query and return a single value."""
        async with self.acquire() as conn:
            return await conn.fetchval(query, *args)


def _generate_cuid() -> str:
    """Generate a CUID-like ID for compatibility with Prisma."""
    return str(uuid.uuid4()).replace("-", "")[:25]


def _record_to_dict(record: Optional[asyncpg.Record]) -> Optional[Dict[str, Any]]:
    """Convert asyncpg Record to dict."""
    if record is None:
        return None
    return dict(record)


def _records_to_list(records: List[asyncpg.Record]) -> List[Dict[str, Any]]:
    """Convert list of asyncpg Records to list of dicts."""
    return [dict(r) for r in records]


class UserManager:
    """Manager for user authentication and device management.

    Maps to Prisma User model with fields:
    - id, username, email, passwordHash, isActive, lastLogin
    - phone, fullName, etc.
    """

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    async def create_user(self, username: str, password_hash: str,
                          email: Optional[str] = None,
                          profile_data: Optional[Dict[str, Any]] = None) -> str:
        """Create a new user and return the user ID."""
        user_id = _generate_cuid()
        now = datetime.datetime.utcnow()  # Use timezone-naive for PostgreSQL 'timestamp without time zone'

        # Extract profile fields if provided
        full_name = None
        phone = None
        medical_conditions = None
        medications = None

        if profile_data:
            full_name = profile_data.get('full_name')
            phone = profile_data.get('phone_number')
            medical_conditions = profile_data.get('medical_conditions')
            medications = profile_data.get('medications')

        await self.db.execute('''
            INSERT INTO "User" (id, username, email, "passwordHash", "isActive",
                               "fullName", phone, "medicalConditions", medications,
                               "createdAt", "updatedAt")
            VALUES ($1, $2, $3, $4, true, $5, $6, $7, $8, $9, $9)
        ''', user_id, username, email, password_hash, full_name, phone,
            json.dumps(medical_conditions) if medical_conditions else None,
            json.dumps(medications) if medications else None,
            now)

        logger.info(f"Created user {user_id} with username {username}")
        return user_id

    async def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """Get user by username."""
        row = await self.db.fetchrow(
            'SELECT * FROM "User" WHERE username = $1',
            username
        )
        return _record_to_dict(row)

    async def get_user_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """Get user by email."""
        row = await self.db.fetchrow(
            'SELECT * FROM "User" WHERE email = $1',
            email
        )
        return _record_to_dict(row)

    async def get_user_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get user by ID."""
        row = await self.db.fetchrow(
            'SELECT * FROM "User" WHERE id = $1',
            user_id
        )
        return _record_to_dict(row)

    async def update_last_login(self, user_id: str) -> None:
        """Update last login timestamp."""
        now = datetime.datetime.utcnow()
        await self.db.execute('''
            UPDATE "User"
            SET "lastLogin" = $1, "updatedAt" = $1
            WHERE id = $2
        ''', now, user_id)
        logger.info(f"Updated last login for user {user_id}")

    async def update_push_token(self, user_id: str, expo_push_token: str) -> None:
        """Save or update the user's Expo push notification token."""
        now = datetime.datetime.utcnow()
        await self.db.execute('''
            UPDATE "User"
            SET "expoPushToken" = $1, "updatedAt" = $2
            WHERE id = $3
        ''', expo_push_token, now, user_id)
        logger.info(f"Updated push token for user {user_id}")

    async def clear_push_token(self, user_id: str) -> None:
        """Clear the user's push token (e.g., on logout)."""
        now = datetime.datetime.utcnow()
        await self.db.execute('''
            UPDATE "User"
            SET "expoPushToken" = NULL, "updatedAt" = $1
            WHERE id = $2
        ''', now, user_id)

    async def deactivate_user(self, user_id: str) -> bool:
        """Deactivate a user account (set isActive to false and clear tokens)."""
        now = datetime.datetime.utcnow()
        result = await self.db.execute('''
            UPDATE "User"
            SET "isActive" = false, "expoPushToken" = NULL, "updatedAt" = $1
            WHERE id = $2
        ''', now, user_id)
        logger.info(f"Deactivated user {user_id}")
        return result is not None

    async def user_exists(self, username: str) -> bool:
        """Check if username exists."""
        count = await self.db.fetchval(
            'SELECT COUNT(*) FROM "User" WHERE username = $1',
            username
        )
        return count > 0

    async def email_exists(self, email: str) -> bool:
        """Check if email exists."""
        count = await self.db.fetchval(
            'SELECT COUNT(*) FROM "User" WHERE email = $1',
            email
        )
        return count > 0

    async def get_profile_by_user_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get user profile data (extracted from User model)."""
        row = await self.db.fetchrow('''
            SELECT id, "fullName", phone, "dateOfBirth", "heightCm", "weightKg",
                   "bloodType", "medicalConditions", medications, email
            FROM "User"
            WHERE id = $1
        ''', user_id)

        if not row:
            return None

        medical_conditions = row['medicalConditions']
        medications = row['medications']

        # Parse JSON fields
        if isinstance(medical_conditions, str):
            try:
                medical_conditions = json.loads(medical_conditions)
            except (json.JSONDecodeError, TypeError):
                medical_conditions = []
        elif medical_conditions is None:
            medical_conditions = []

        if isinstance(medications, str):
            try:
                medications = json.loads(medications)
            except (json.JSONDecodeError, TypeError):
                medications = []
        elif medications is None:
            medications = []

        return {
            'id': row['id'],  # Use user_id as profile id (profile is embedded in User)
            'full_name': row['fullName'],
            'phone_number': row['phone'],
            'email': row['email'],
            'date_of_birth': row['dateOfBirth'].isoformat() if row['dateOfBirth'] else None,
            'height_cm': row['heightCm'],
            'weight_kg': row['weightKg'],
            'blood_type': row['bloodType'],
            'medical_conditions': medical_conditions,
            'medications': medications,
        }

    # --- Device management ---

    async def _ensure_device_exists(self, conn: Connection, device_id: str,
                                     device_type: str = 'PENDANT',
                                     product_id: Optional[str] = None) -> None:
        """Ensure a Device record exists for the given device ID."""
        existing = await conn.fetchval(
            'SELECT id FROM "Device" WHERE "deviceId" = $1',
            device_id
        )
        if not existing:
            internal_id = _generate_cuid()
            now = datetime.datetime.utcnow()
            # Map device_type to enum
            device_type_enum = device_type.upper() if device_type else 'PENDANT'
            if device_type_enum not in ('PENDANT', 'HUB'):
                device_type_enum = 'PENDANT'

            await conn.execute('''
                INSERT INTO "Device" (id, "deviceId", "deviceType", "productId", "createdAt", "updatedAt")
                VALUES ($1, $2, $3::"DeviceType", $4, $5, $5)
            ''', internal_id, device_id, device_type_enum, product_id, now)
            logger.info(f"Created Device record for {device_id} (type={device_type_enum})")

    async def link_device_to_user(self, user_id: str, device_id: str,
                                   device_type: str = 'PENDANT',
                                   label: Optional[str] = None,
                                   product_id: Optional[str] = None) -> Dict[str, Any]:
        """Link a device to a user. Creates Device record if needed."""
        async with self.db.acquire() as conn:
            # Ensure device exists
            await self._ensure_device_exists(conn, device_id, device_type, product_id)

            # Check if already linked
            existing = await conn.fetchrow('''
                SELECT * FROM "UserDevice"
                WHERE "userId" = $1 AND "eviewDeviceId" = $2
            ''', user_id, device_id)

            now = datetime.datetime.utcnow()

            if existing:
                # Update label
                await conn.execute('''
                    UPDATE "UserDevice"
                    SET label = $1
                    WHERE "userId" = $2 AND "eviewDeviceId" = $3
                ''', label, user_id, device_id)
            else:
                # Create new link
                link_id = _generate_cuid()
                await conn.execute('''
                    INSERT INTO "UserDevice" (id, "userId", "eviewDeviceId", label, "isPrimary", "assignedAt")
                    VALUES ($1, $2, $3, $4, false, $5)
                ''', link_id, user_id, device_id, label, now)
                logger.info(f"Linked device {device_id} to user {user_id}")

            # Return the link
            row = await conn.fetchrow('''
                SELECT ud.*, d."deviceType", d."deviceName"
                FROM "UserDevice" ud
                JOIN "Device" d ON d."deviceId" = ud."eviewDeviceId"
                WHERE ud."userId" = $1 AND ud."eviewDeviceId" = $2
            ''', user_id, device_id)

            return {
                "id": row["id"],
                "user_id": row["userId"],
                "device_id": row["eviewDeviceId"],
                "device_type": row["deviceType"],
                "label": row["label"],
                "linked_at": row["assignedAt"].isoformat() if row["assignedAt"] else None,
            }

    async def list_user_devices(self, user_id: str,
                                 device_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all devices linked to a user, optionally filtered by device type."""
        if device_type:
            # Normalize device_type
            device_type_filter = device_type.upper()
            if device_type_filter not in ('PENDANT', 'HUB'):
                device_type_filter = 'PENDANT'

            rows = await self.db.fetch('''
                SELECT ud.*, d."deviceType", d."deviceName"
                FROM "UserDevice" ud
                JOIN "Device" d ON d."deviceId" = ud."eviewDeviceId"
                WHERE ud."userId" = $1 AND d."deviceType" = $2::"DeviceType"
                ORDER BY ud."assignedAt" ASC
            ''', user_id, device_type_filter)
        else:
            rows = await self.db.fetch('''
                SELECT ud.*, d."deviceType", d."deviceName"
                FROM "UserDevice" ud
                JOIN "Device" d ON d."deviceId" = ud."eviewDeviceId"
                WHERE ud."userId" = $1
                ORDER BY ud."assignedAt" ASC
            ''', user_id)

        return [{
            "id": row["id"],
            "user_id": row["userId"],
            "device_id": row["eviewDeviceId"],
            "device_type": row["deviceType"],
            "device_name": row["deviceName"],
            "label": row["label"],
            "is_primary": row["isPrimary"],
            "linked_at": row["assignedAt"].isoformat() if row["assignedAt"] else None,
        } for row in rows]

    async def unlink_device_from_user(self, user_id: str, device_id: str) -> bool:
        """Unlink a device from a user."""
        result = await self.db.execute('''
            DELETE FROM "UserDevice"
            WHERE "userId" = $1 AND "eviewDeviceId" = $2
        ''', user_id, device_id)
        removed = "DELETE 1" in result
        if removed:
            logger.info(f"Unlinked device {device_id} from user {user_id}")
        return removed

    async def get_device_owners(self, device_id: str) -> List[str]:
        """Get all user IDs that have this device linked."""
        rows = await self.db.fetch('''
            SELECT "userId" FROM "UserDevice"
            WHERE "eviewDeviceId" = $1
            ORDER BY "assignedAt" ASC
        ''', device_id)
        return [row["userId"] for row in rows]


class EviewEventManager:
    """Manager for Eview button device events.

    Maps to Prisma EviewEvent model.
    """

    BUTTON_TYPES = {
        12: "SOS Button",
        13: "Side Call Button 1",
        14: "Side Call Button 2",
        17: "SOS Ending",
        11: "SOS Stop"
    }

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    def parse_button_type(self, alarm_code: Optional[int]) -> Optional[str]:
        """Parse alarm code to identify button type."""
        if alarm_code is None:
            return None
        for bit, button_name in self.BUTTON_TYPES.items():
            if alarm_code & (1 << bit):
                return button_name
        return None

    async def _ensure_device_exists(self, conn: Connection, device_id: str) -> None:
        """Ensure a Device record exists."""
        existing = await conn.fetchval(
            'SELECT id FROM "Device" WHERE "deviceId" = $1',
            device_id
        )
        if not existing:
            internal_id = _generate_cuid()
            now = datetime.datetime.utcnow()
            await conn.execute('''
                INSERT INTO "Device" (id, "deviceId", "deviceType", "createdAt", "updatedAt")
                VALUES ($1, $2, 'PENDANT', $3, $3)
            ''', internal_id, device_id, now)

    # Deduplication window: skip saving if an identical event was saved within this period.
    DEDUP_WINDOW_SECONDS = 60

    async def save_event(self, device_id: str, event_type: str, timestamp: datetime.datetime,
                         event_data: Dict[str, Any]) -> Optional[str]:
        """Save an Eview device event, with deduplication.

        Returns the event_id if saved, or None if skipped as duplicate.
        """
        nested_data = event_data.get('data', {})
        general_data = nested_data.get('generalData', {})
        location_data = nested_data.get('latestLocation', {})
        headers = event_data.get('headers', {})

        # Simplified alarm payloads (geo1, fallDown, etc.) put lat/lng directly
        # in nested_data instead of inside latestLocation
        if not location_data.get('lat') and nested_data.get('lat'):
            location_data = nested_data

        alarm_code = general_data.get('statusCode')
        button_type = self.parse_button_type(alarm_code)

        now = datetime.datetime.utcnow()
        dedup_cutoff = now - datetime.timedelta(seconds=self.DEDUP_WINDOW_SECONDS)

        async with self.db.acquire() as conn:
            async with conn.transaction():
                await self._ensure_device_exists(conn, device_id)

                # Advisory lock keyed on (device_id, event_type) to prevent TOCTOU race
                # between the dedup SELECT and the INSERT.
                lock_key = hash((device_id, event_type)) & 0x7FFFFFFF
                await conn.execute('SELECT pg_advisory_xact_lock($1)', lock_key)

                # Dedup: skip if same device + event type + statusCode within window
                existing = await conn.fetchval('''
                    SELECT id FROM "EviewEvent"
                    WHERE "eviewDeviceId" = $1
                      AND "eventType" = $2
                      AND "statusCode" IS NOT DISTINCT FROM $3
                      AND "createdAt" > $4
                    LIMIT 1
                ''', device_id, event_type, general_data.get('statusCode'), dedup_cutoff)

                if existing:
                    logger.debug(
                        f"Dedup: skipping duplicate {event_type} for device {device_id} "
                        f"(matches {existing} within {self.DEDUP_WINDOW_SECONDS}s)"
                    )
                    return None

                event_id = _generate_cuid()

                await conn.execute('''
                    INSERT INTO "EviewEvent"
                    (id, "eviewDeviceId", "eventType", timestamp, "deviceName", "batteryLevel",
                     lat, lng, "accuracyMeters", "isGps", "isWifi", "isGsm", "isMotion", "isCharging",
                     "workMode", "signalStrength", "statusCode", "statusCode2", "alarmCode",
                     "alarmCodeExtend", "buttonType", "rawPayload", "processedAt", "createdAt")
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16,
                            $17, $18, $19, $20, $21, $22, $23, $23)
                ''',
                    event_id,
                    device_id,
                    event_type,
                    timestamp,
                    headers.get('deviceName'),
                    general_data.get('battery'),
                    location_data.get('lat'),
                    location_data.get('lng'),
                    location_data.get('radius'),
                    general_data.get('isGPS', False),
                    general_data.get('isWIFI', False),
                    general_data.get('isGSM', False),
                    general_data.get('isMotion', False),
                    general_data.get('isCharging', False),
                    general_data.get('workMode'),
                    general_data.get('signalSize'),
                    general_data.get('statusCode'),
                    general_data.get('statusCode2'),
                    alarm_code,
                    general_data.get('alarmCodeExtend'),
                    button_type,
                    json.dumps(event_data),
                    now
                )

        logger.info(f"Saved Eview event {event_id} for device {device_id}: type={event_type}, button={button_type}")
        return event_id

    async def get_latest_event(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Get the latest event for a device."""
        row = await self.db.fetchrow('''
            SELECT * FROM "EviewEvent"
            WHERE "eviewDeviceId" = $1
            ORDER BY timestamp DESC
            LIMIT 1
        ''', device_id)

        if row:
            result = dict(row)
            if result.get('rawPayload'):
                try:
                    result['rawPayload'] = json.loads(result['rawPayload'])
                except (json.JSONDecodeError, TypeError):
                    pass
            return result
        return None

    async def get_events_by_device(self, device_id: str,
                                    start_date: Optional[datetime.datetime] = None,
                                    end_date: Optional[datetime.datetime] = None,
                                    event_type: Optional[str] = None,
                                    event_types: Optional[List[str]] = None,
                                    limit: int = 100,
                                    offset: int = 0) -> List[Dict[str, Any]]:
        """Get events for a specific device."""
        query = '''
            SELECT * FROM "EviewEvent"
            WHERE "eviewDeviceId" = $1 AND "eventType" != 'realtime_fetch'
        '''
        params: List[Any] = [device_id]
        param_idx = 2

        if start_date:
            query += f" AND timestamp >= ${param_idx}"
            params.append(start_date)
            param_idx += 1

        if end_date:
            query += f" AND timestamp <= ${param_idx}"
            params.append(end_date)
            param_idx += 1

        # Support both single event_type and list of event_types
        if event_types:
            placeholders = ', '.join(f'${param_idx + i}' for i in range(len(event_types)))
            query += f' AND "eventType" IN ({placeholders})'
            params.extend(event_types)
            param_idx += len(event_types)
        elif event_type:
            query += f' AND "eventType" = ${param_idx}'
            params.append(event_type)
            param_idx += 1

        query += f" ORDER BY timestamp DESC LIMIT ${param_idx} OFFSET ${param_idx + 1}"
        params.extend([limit, offset])

        rows = await self.db.fetch(query, *params)
        results = []
        for row in rows:
            result = dict(row)
            if result.get('rawPayload'):
                try:
                    result['rawPayload'] = json.loads(result['rawPayload'])
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append(result)
        return results

    async def get_button_press_events(self, device_id: Optional[str] = None,
                                       start_date: Optional[datetime.datetime] = None,
                                       end_date: Optional[datetime.datetime] = None,
                                       limit: int = 100) -> List[Dict[str, Any]]:
        """Get button press events (where buttonType is not null)."""
        query = 'SELECT * FROM "EviewEvent" WHERE "buttonType" IS NOT NULL'
        params: List[Any] = []
        param_idx = 1

        if device_id:
            query += f' AND "eviewDeviceId" = ${param_idx}'
            params.append(device_id)
            param_idx += 1

        if start_date:
            query += f" AND timestamp >= ${param_idx}"
            params.append(start_date)
            param_idx += 1

        if end_date:
            query += f" AND timestamp <= ${param_idx}"
            params.append(end_date)
            param_idx += 1

        query += f" ORDER BY timestamp DESC LIMIT ${param_idx}"
        params.append(limit)

        rows = await self.db.fetch(query, *params)
        results = []
        for row in rows:
            result = dict(row)
            if result.get('rawPayload'):
                try:
                    result['rawPayload'] = json.loads(result['rawPayload'])
                except (json.JSONDecodeError, TypeError):
                    pass
            results.append(result)
        return results

    async def get_device_status(self, device_id: str) -> Optional[Dict[str, Any]]:
        """Get current status of a device based on latest event."""
        latest = await self.get_latest_event(device_id)
        if not latest:
            return None

        online = False
        last_seen = latest.get('timestamp')
        if last_seen:
            try:
                if isinstance(last_seen, datetime.datetime):
                    last_seen_dt = last_seen
                else:
                    last_seen_dt = datetime.datetime.fromisoformat(str(last_seen).replace('Z', '+00:00'))
                now = datetime.datetime.utcnow()
                # Ensure both datetimes are timezone-naive for comparison
                if last_seen_dt.tzinfo is not None:
                    last_seen_dt = last_seen_dt.replace(tzinfo=None)
                age_minutes = (now - last_seen_dt).total_seconds() / 60
                online = age_minutes < 10
            except (ValueError, TypeError):
                online = True

        return {
            "device_id": device_id,
            "device_name": latest.get('deviceName'),
            "online": online,
            "last_seen": last_seen.isoformat() if isinstance(last_seen, datetime.datetime) else last_seen,
            "battery": latest.get('batteryLevel'),
            "latitude": latest.get('lat'),
            "longitude": latest.get('lng'),
            "accuracy_meters": latest.get('accuracyMeters'),
            "is_gps": latest.get('isGps'),
            "is_wifi": latest.get('isWifi'),
            "is_gsm": latest.get('isGsm'),
            "is_motion": latest.get('isMotion'),
            "is_charging": latest.get('isCharging'),
            "work_mode": latest.get('workMode'),
            "signal_strength": latest.get('signalStrength'),
        }

    async def get_statistics(self, device_id: Optional[str] = None,
                              start_date: Optional[datetime.datetime] = None,
                              end_date: Optional[datetime.datetime] = None) -> Dict[str, Any]:
        """Get event statistics."""
        base_where = "1=1"
        params: List[Any] = []
        param_idx = 1

        if device_id:
            base_where += f' AND "eviewDeviceId" = ${param_idx}'
            params.append(device_id)
            param_idx += 1

        if start_date:
            base_where += f" AND timestamp >= ${param_idx}"
            params.append(start_date)
            param_idx += 1

        if end_date:
            base_where += f" AND timestamp <= ${param_idx}"
            params.append(end_date)
            param_idx += 1

        total_events = await self.db.fetchval(
            f'SELECT COUNT(*) FROM "EviewEvent" WHERE {base_where}',
            *params
        )

        button_press_count = await self.db.fetchval(
            f'SELECT COUNT(*) FROM "EviewEvent" WHERE {base_where} AND "buttonType" IS NOT NULL',
            *params
        )

        events_by_type = await self.db.fetch(
            f'''SELECT "eventType", COUNT(*) as count
                FROM "EviewEvent"
                WHERE {base_where}
                GROUP BY "eventType"''',
            *params
        )

        events_by_button = await self.db.fetch(
            f'''SELECT "buttonType", COUNT(*) as count
                FROM "EviewEvent"
                WHERE {base_where} AND "buttonType" IS NOT NULL
                GROUP BY "buttonType"''',
            *params
        )

        return {
            'total_events': total_events,
            'button_press_count': button_press_count,
            'events_by_type': _records_to_list(events_by_type),
            'events_by_button': _records_to_list(events_by_button),
        }


class GeofenceManager:
    """Manager for geofence configurations.

    Maps to Prisma Geofence model.
    """

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    async def create_geofence(self, user_id: str, device_id: str, zone_number: int,
                               name: str, center_lat: float, center_lng: float,
                               radius_meters: int, direction: str = 'BOTH',
                               detect_interval_seconds: int = 300,
                               enabled: bool = True) -> str:
        """Create a new geofence."""
        geofence_id = _generate_cuid()
        now = datetime.datetime.utcnow()

        # Map direction to enum
        direction_enum = direction.upper()
        if direction_enum not in ('ENTER', 'LEAVE', 'BOTH'):
            direction_enum = 'BOTH'

        await self.db.execute('''
            INSERT INTO "Geofence"
            (id, "userId", "eviewDeviceId", "zoneNumber", name, "centerLat", "centerLng",
             "radiusMeters", direction, "detectIntervalSeconds", "isActive", "syncedToDevice",
             "createdAt", "updatedAt")
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::\"GeofenceDirection\", $10, $11, false, $12, $12)
        ''', geofence_id, user_id, device_id, zone_number, name, center_lat, center_lng,
            radius_meters, direction_enum, detect_interval_seconds, enabled, now)

        logger.info(f"Created geofence {geofence_id}: zone {zone_number} for device {device_id}")
        return geofence_id

    def _geofence_to_dict(self, row: asyncpg.Record) -> Dict[str, Any]:
        """Convert geofence record to dict with consistent naming."""
        return {
            "id": row["id"],
            "user_id": row["userId"],
            "device_id": row["eviewDeviceId"],
            "zone_number": row["zoneNumber"],
            "name": row["name"],
            "center_lat": row["centerLat"],
            "center_lng": row["centerLng"],
            "radius_meters": row["radiusMeters"],
            "direction": row["direction"],
            "detect_interval_seconds": row.get("detectIntervalSeconds", 300),
            "enabled": row.get("isActive", True),
            "synced_to_device": row.get("syncedToDevice", False),
            "last_synced_at": row["lastSyncedAt"].isoformat() if row.get("lastSyncedAt") else None,
            "created_at": row["createdAt"].isoformat() if row.get("createdAt") else None,
        }

    async def get_geofences(self, device_id: str) -> List[Dict[str, Any]]:
        """Get all geofences for a device."""
        rows = await self.db.fetch('''
            SELECT * FROM "Geofence"
            WHERE "eviewDeviceId" = $1
            ORDER BY "zoneNumber"
        ''', device_id)
        return [self._geofence_to_dict(row) for row in rows]

    async def get_geofence(self, device_id: str, zone_number: int) -> Optional[Dict[str, Any]]:
        """Get a specific geofence."""
        row = await self.db.fetchrow('''
            SELECT * FROM "Geofence"
            WHERE "eviewDeviceId" = $1 AND "zoneNumber" = $2
        ''', device_id, zone_number)
        if row:
            return self._geofence_to_dict(row)
        return None

    async def update_geofence(self, device_id: str, zone_number: int, **fields) -> bool:
        """Update geofence fields."""
        # Map both snake_case and camelCase to database columns
        allowed_fields = {
            'name': 'name',
            'isActive': '"isActive"',
            'is_active': '"isActive"',
            'enabled': '"isActive"',
            'centerLat': '"centerLat"',
            'center_lat': '"centerLat"',
            'centerLng': '"centerLng"',
            'center_lng': '"centerLng"',
            'radiusMeters': '"radiusMeters"',
            'radius_meters': '"radiusMeters"',
            'direction': 'direction',
            'detectIntervalSeconds': '"detectIntervalSeconds"',
            'detect_interval_seconds': '"detectIntervalSeconds"',
            'syncedToDevice': '"syncedToDevice"',
            'synced_to_device': '"syncedToDevice"',
            'lastSyncedAt': '"lastSyncedAt"',
            'last_synced_at': '"lastSyncedAt"'
        }

        update_parts = []
        params: List[Any] = []
        param_idx = 1

        for key, value in fields.items():
            if key in allowed_fields:
                col = allowed_fields[key]
                if key == 'direction':
                    update_parts.append(f'{col} = ${param_idx}::"GeofenceDirection"')
                else:
                    update_parts.append(f'{col} = ${param_idx}')
                params.append(value)
                param_idx += 1

        if not update_parts:
            return False

        # Add updatedAt
        update_parts.append(f'"updatedAt" = ${param_idx}')
        params.append(datetime.datetime.utcnow())
        param_idx += 1

        # Mark as unsynced unless explicitly syncing
        if 'syncedToDevice' not in fields:
            update_parts.append(f'"syncedToDevice" = false')

        params.extend([device_id, zone_number])
        query = f'''
            UPDATE "Geofence"
            SET {", ".join(update_parts)}
            WHERE "eviewDeviceId" = ${param_idx} AND "zoneNumber" = ${param_idx + 1}
        '''

        result = await self.db.execute(query, *params)
        updated = "UPDATE 1" in result
        if updated:
            logger.info(f"Updated geofence zone {zone_number} for device {device_id}")
        return updated

    async def delete_geofence(self, device_id: str, zone_number: int) -> bool:
        """Delete a geofence."""
        result = await self.db.execute('''
            DELETE FROM "Geofence"
            WHERE "eviewDeviceId" = $1 AND "zoneNumber" = $2
        ''', device_id, zone_number)
        deleted = "DELETE 1" in result
        if deleted:
            logger.info(f"Deleted geofence zone {zone_number} for device {device_id}")
        return deleted

    async def mark_synced(self, device_id: str, zone_number: int) -> None:
        """Mark a geofence as synced."""
        await self.update_geofence(
            device_id, zone_number,
            syncedToDevice=True,
            lastSyncedAt=datetime.datetime.utcnow()
        )

    async def get_next_available_zone(self, device_id: str) -> Optional[int]:
        """Get the next available zone number (1-4)."""
        rows = await self.db.fetch('''
            SELECT "zoneNumber" FROM "Geofence"
            WHERE "eviewDeviceId" = $1
        ''', device_id)
        used_zones = {row['zoneNumber'] for row in rows}

        for zone in range(1, 5):
            if zone not in used_zones:
                return zone
        return None


class DeviceSettingsManager:
    """Manager for device settings.

    Settings are stored directly on the Device model in Prisma.
    """

    def __init__(self, db_manager: DatabaseManager):
        self.db = db_manager

    async def get_settings(self, device_id: str) -> Dict[str, Any]:
        """Get device settings."""
        row = await self.db.fetchrow('''
            SELECT "deviceId", "fallDetectionEnabled", "fallSensitivity",
                   "batteryThreshold", "fallDialEnabled"
            FROM "Device"
            WHERE "deviceId" = $1
        ''', device_id)

        if row:
            return {
                'device_id': row['deviceId'],
                'fall_detection_enabled': row['fallDetectionEnabled'],
                'fall_sensitivity': row['fallSensitivity'],
                'battery_threshold': row['batteryThreshold'],
                'fall_dial_enabled': row['fallDialEnabled'],
            }

        # Return defaults
        return {
            'device_id': device_id,
            'fall_detection_enabled': True,
            'fall_sensitivity': 5,
            'battery_threshold': 20,
            'fall_dial_enabled': True,
        }

    async def upsert_settings(self, device_id: str, **fields) -> None:
        """Create or update device settings."""
        # Map both snake_case and camelCase to database columns
        allowed_fields = {
            'fall_detection_enabled': '"fallDetectionEnabled"',
            'fallDetectionEnabled': '"fallDetectionEnabled"',
            'fall_sensitivity': '"fallSensitivity"',
            'fallSensitivity': '"fallSensitivity"',
            'battery_threshold': '"batteryThreshold"',
            'batteryThreshold': '"batteryThreshold"',
            'fall_dial_enabled': '"fallDialEnabled"',
            'fallDialEnabled': '"fallDialEnabled"',
        }

        update_parts = []
        params: List[Any] = []
        param_idx = 1

        for key, value in fields.items():
            if key in allowed_fields:
                col = allowed_fields[key]
                update_parts.append(f'{col} = ${param_idx}')
                params.append(value)
                param_idx += 1

        if not update_parts:
            return

        # Add updatedAt
        update_parts.append(f'"updatedAt" = ${param_idx}')
        now = datetime.datetime.utcnow()
        params.append(now)
        param_idx += 1

        # Check if device exists
        exists = await self.db.fetchval(
            'SELECT id FROM "Device" WHERE "deviceId" = $1',
            device_id
        )

        if exists:
            params.append(device_id)
            query = f'''
                UPDATE "Device"
                SET {", ".join(update_parts)}
                WHERE "deviceId" = ${param_idx}
            '''
            await self.db.execute(query, *params)
        else:
            # Create device with settings
            internal_id = _generate_cuid()
            await self.db.execute('''
                INSERT INTO "Device"
                (id, "deviceId", "deviceType", "fallDetectionEnabled", "fallSensitivity",
                 "batteryThreshold", "fallDialEnabled", "createdAt", "updatedAt")
                VALUES ($1, $2, 'PENDANT', $3, $4, $5, $6, $7, $7)
            ''',
                internal_id,
                device_id,
                fields.get('fall_detection_enabled', True),
                fields.get('fall_sensitivity', 5),
                fields.get('battery_threshold', 20),
                fields.get('fall_dial_enabled', True),
                now
            )

        logger.info(f"Updated settings for device {device_id}")
