"""Prometheus counters. Each process has its own registry: Celery worker counters
(deliveries) are only visible from the worker process, not the API's /metrics."""

from prometheus_client import CONTENT_TYPE_LATEST, Counter, generate_latest

EVENTS_RECEIVED = Counter("ha_events_received", "Sound events received", ["sound"])
EVENTS_MERGED = Counter("ha_events_merged", "Events merged into another sensor's recent event")
ALERTS_CREATED = Counter("ha_alerts_created", "Alerts created from events", ["sound"])
DELIVERIES = Counter("ha_deliveries", "Alert deliveries by outcome", ["channel", "status"])
DEVICES_OFFLINE = Counter("ha_devices_offline", "Devices marked offline")
ALERTS_STALE = Counter("ha_alerts_stale", "Undelivered alerts failed by the stale sweep")


def render() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
