"""Webhook delivery helper — enqueues a Celery deliver_webhook task after DB commit."""

import ipaddress
import urllib.parse
import uuid

import structlog

logger = structlog.get_logger()

# RFC-1918 + loopback + link-local + cloud metadata ranges
_BLOCKED_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def validate_webhook_url(url: str, require_https: bool = True) -> None:
    """Raise ValueError if the URL is not a safe external target."""
    parsed = urllib.parse.urlparse(url)
    if require_https and parsed.scheme != "https":
        raise ValueError(f"Webhook URL must use https (got {parsed.scheme!r})")
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Webhook URL scheme must be http or https (got {parsed.scheme!r})")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Webhook URL missing hostname")
    if hostname.lower() in ("localhost", "localhost.localdomain"):
        raise ValueError("Webhook URL must not target localhost")
    try:
        addr = ipaddress.ip_address(hostname)
        for net in _BLOCKED_NETS:
            if addr in net:
                raise ValueError(f"Webhook URL targets a private/reserved address ({hostname})")
    except ValueError as exc:
        if "private" in str(exc) or "reserved" in str(exc):
            raise
        # Not a literal IP — hostname; SSRF mitigated by egress firewall


def enqueue_webhook(
    *,
    document_id: str,
    tenant_id: str,
    final_status: str,
    confidence_score: float | None,
    ocr_engine: str | None,
) -> None:
    """Enqueue the deliver_webhook Celery task. Called after DB commit in orchestrator."""
    from celery_app import app as celery_app

    delivery_id = str(uuid.uuid4())
    celery_app.send_task(
        "tasks.deliver_webhook",
        args=[delivery_id, document_id, tenant_id, "document.completed", final_status, confidence_score, ocr_engine],
        queue="ocr",
    )
    logger.info("webhook.enqueued", document_id=document_id, delivery_id=delivery_id)
