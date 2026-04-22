import logging
import os

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

logger = logging.getLogger(__name__)


def init_sentry() -> None:
    """Initialize Sentry error monitoring.

    Reads configuration from environment variables:
      - SENTRY_DSN: Required to enable Sentry. If empty/missing, Sentry is
        silently disabled (safe for local development).
      - SENTRY_ENVIRONMENT: Deployment environment tag (default: "development").
      - SENTRY_TRACES_SAMPLE_RATE: Fraction of transactions to capture for
        performance monitoring (default: 0.1 = 10%).
    """
    dsn = os.getenv("SENTRY_DSN", "")
    if not dsn:
        logger.info("SENTRY_DSN not set — Sentry disabled")
        return

    environment = os.getenv("SENTRY_ENVIRONMENT", "development")
    traces_sample_rate = float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1"))

    sentry_sdk.init(
        dsn=dsn,
        environment=environment,
        traces_sample_rate=traces_sample_rate,
        send_default_pii=True,
        integrations=[
            FastApiIntegration(transaction_style="endpoint"),
            StarletteIntegration(transaction_style="endpoint"),
        ],
    )
    logger.info(
        "Sentry initialized (env=%s, traces_sample_rate=%.2f)",
        environment,
        traces_sample_rate,
    )
