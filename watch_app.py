import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.openapi.utils import get_openapi
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

# Sentry (must init before FastAPI app is created)
from core.sentry import init_sentry

# Database
from core.database import (
    DatabaseManager, UserManager,
    EviewEventManager, GeofenceManager, DeviceSettingsManager,
)

# Eview MQTT
from eview.mqtt_service import EviewMQTTService
from eview.mqtt_startup import start_mqtt_service

# EVMars API client
from eview.evmars_client import get_evmars_client

# Route modules
from auth.routes import router as auth_router
from eview.routes import router as eview_router
from eview.config_routes import router as eview_config_router

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

init_sentry()

# ====================
# Global instances
# ====================

db_manager = DatabaseManager()
user_manager = UserManager(db_manager)
eview_event_manager = EviewEventManager(db_manager)
geofence_manager = GeofenceManager(db_manager)
device_settings_manager = DeviceSettingsManager(db_manager)
evmars_client = get_evmars_client()

# Eview MQTT service (initialized on startup)
eview_mqtt_service: Optional[EviewMQTTService] = None

# ====================
# Lifespan (startup/shutdown)
# ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown."""
    global eview_mqtt_service

    # === STARTUP ===
    # Initialize database connection pool
    logger.info("Initializing PostgreSQL connection pool...")
    await db_manager.init_pool()
    logger.info("Database pool initialized successfully")

    auto_start_mqtt = os.getenv('EVIEW_MQTT_AUTO_START', 'true').lower() == 'true'

    if auto_start_mqtt:
        logger.info("Auto-starting Eview MQTT service...")
        eview_mqtt_service = await start_mqtt_service(eview_event_manager, db_manager)
    else:
        logger.info("Eview MQTT auto-start disabled (EVIEW_MQTT_AUTO_START=false)")

    yield  # Application is running

    # === SHUTDOWN ===
    logger.info("Shutting down...")

    # Stop MQTT service
    if eview_mqtt_service:
        eview_mqtt_service.stop()

    # Close database pool
    await db_manager.close()
    logger.info("Database pool closed")


# ====================
# FastAPI Application
# ====================

tags_metadata = [
    {"name": "auth", "description": "Authentication endpoints for user login and signup."},
    {"name": "system", "description": "Health checks and API schema."},
    {"name": "devices", "description": "Eview device management, linking, and status."},
    {"name": "eview", "description": "Eview button events, MQTT service, and real-time data."},
    {"name": "device-config", "description": "Device configuration: fall detection, geofences, battery alerts."},
    {"name": "device-alerts", "description": "Unified device alerts and notifications."},
]

app = FastAPI(
    title="Sensu API",
    version="2.0.0",
    description=(
        "API for Eview personal alarm button devices. "
        "Provides device management, real-time MQTT events, fall detection, "
        "geofencing, battery alerts, and user authentication."
    ),
    openapi_tags=tags_metadata,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    contact={
        "name": "Sensu",
        "url": "https://example.com",
        "email": "support@example.com",
    },
    license_info={
        "name": "MIT License",
        "url": "https://opensource.org/licenses/MIT",
    },
)

# CORS — locked to known origins (patched 2026-04-16 Ustym)
_CORS_ORIGINS = [o.strip() for o in os.environ.get('CORS_ALLOW_ORIGINS','').split(',') if o.strip()] or [
    'https://pay.sensu.com.mx',
    'https://api.sensu.com.mx',
    'https://sensu.com.mx',
    'http://localhost:8081',
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=['GET','POST','PUT','PATCH','DELETE','OPTIONS'],
    allow_headers=['Authorization','Content-Type','Accept','Origin'],
    max_age=600,
)

# CORS headers on 401/403: Starlette HTTPBearer raises 403 before CORSMiddleware wraps it.
from fastapi import Request as _Request
from fastapi.exceptions import HTTPException as _HTTPException
from fastapi.responses import JSONResponse as _JSONResponse

@app.exception_handler(_HTTPException)
async def _cors_http_exc_handler(request: _Request, exc: _HTTPException):
    origin = request.headers.get("origin", "")
    headers = {}
    if origin in _CORS_ORIGINS:
        headers["Access-Control-Allow-Origin"] = origin
        headers["Access-Control-Allow-Credentials"] = "true"
        headers["Vary"] = "Origin"
    if exc.headers:
        headers.update(exc.headers)
    from fastapi.responses import JSONResponse as JR
    return JR(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=headers,
    )



# ====================
# OpenAPI customization
# ====================

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        tags=tags_metadata,
    )

    # Add JWT Bearer authentication
    openapi_schema.setdefault("components", {})
    openapi_schema["components"].setdefault("securitySchemes", {})
    openapi_schema["components"]["securitySchemes"]["bearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": "JWT token obtained from /api/auth/login endpoint"
    }

    # Global security requirement
    openapi_schema["security"] = [{"bearerAuth": []}]

    # Make specific endpoints public (no auth)
    public_paths = {"/api/health", "/api/auth/signup", "/api/auth/login", "/api/auth/refresh"}
    if "paths" in openapi_schema:
        for path, methods in openapi_schema["paths"].items():
            if path in public_paths:
                for method in methods.values():
                    if isinstance(method, dict):
                        method["security"] = []

    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi  # type: ignore


# ====================
# Health check
# ====================

@app.get("/api/health", tags=["system"], summary="API health check")
async def api_health():
    """Check API health and database connection."""
    try:
        # Quick database check
        pool_status = "connected" if db_manager._pool else "disconnected"
        return {"status": "ok", "database": pool_status}
    except Exception as e:
        return {"status": "degraded", "database": "error", "error": str(e)}


# ====================
# Sentry debug
# ====================

@app.get("/api/sentry-debug", tags=["system"], summary="Trigger a test error for Sentry")
async def sentry_debug():
    """Raises an exception to verify Sentry is capturing errors."""
    _ = 1 / 0


# ====================
# Include routers
# ====================

app.include_router(auth_router)
app.include_router(eview_router)
app.include_router(eview_config_router)
import os; os.makedirs("/app/static/avatars", exist_ok=True); app.mount("/static", StaticFiles(directory="/app/static"), name="static")


# ====================
# OpenAPI YAML export
# ====================

@app.get("/openapi.yaml", include_in_schema=False)
def openapi_yaml():
    schema = app.openapi()
    try:
        import yaml  # type: ignore
        return PlainTextResponse(yaml.safe_dump(schema, sort_keys=False), media_type="application/yaml")
    except Exception:
        return JSONResponse(schema)


# ====================
# Entry point
# ====================

def main():
    """Run the Sensu API server."""
    logging.getLogger().setLevel(logging.INFO)
    logger.info("Starting Sensu API on http://0.0.0.0:8001 ...")
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="info")


if __name__ == "__main__":
    main()
