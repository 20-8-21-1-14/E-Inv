"""Non-blocking webhook delivery. Fire-and-forget — never fails document processing."""

import asyncio
import ipaddress
import urllib.parse
from datetime import datetime, timezone

import httpx
import structlog

logger = structlog.get_logger()

_TIMEOUT = 10.0
_RETRIES = 3
_RETRY_DELAY = 2.0

# RFC-1918 + link-local + loopback networks that must never be targets of outbound webhook calls
_BLOCKED_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),            # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),           # IPv6 ULA
    ipaddress.ip_network("fe80::/10"),          # IPv6 link-local
]


def _validate_webhook_url(url: str) -> None:
    """Raise ValueError if the URL is not a safe external HTTP(S) target.

    Blocks private IP ranges to prevent SSRF attacks where a malicious tenant
    could use the OCR worker to probe internal services.
    Does not perform DNS resolution — hostname-based bypass is an accepted
    residual risk; mitigate further with egress firewall rules on the host.
    """
    # urlparse never raises — no try/except needed
    parsed = urllib.parse.urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"Webhook URL scheme must be http or https (got {parsed.scheme!r})"
        )

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Webhook URL is missing a hostname")

    if hostname.lower() in ("localhost", "localhost.localdomain"):
        raise ValueError("Webhook URL must not target localhost")

    # Only check literal IP addresses. For hostnames, DNS resolution happens at
    # request time — mitigate DNS-based SSRF with egress firewall rules.
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        return  # hostname is not a literal IP — safe to proceed
    # else: hostname IS a literal IP — check against blocked ranges
    for net in _BLOCKED_NETS:
        if addr in net:
            raise ValueError(
                f"Webhook URL targets a private/reserved address ({hostname})"
            )


async def dispatch(
    webhook_url: str,
    document_id: str,
    tenant_id: str,
    status: str,
    confidence_score: float | None,
    ocr_engine: str | None,
) -> None:
    try:
        _validate_webhook_url(webhook_url)
    except ValueError as exc:
        logger.warning("webhook.blocked_invalid_url", document_id=document_id, error=str(exc))
        return

    payload = {
        "event": "document.processed",
        "document_id": document_id,
        "tenant_id": tenant_id,
        "status": status,
        "confidence_score": confidence_score,
        "ocr_engine": ocr_engine,
        "result_url": f"/v1/documents/{document_id}/result",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    for attempt in range(1, _RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(webhook_url, json=payload)
                resp.raise_for_status()
            logger.info("webhook.delivered", document_id=document_id, attempt=attempt)
            return
        except httpx.HTTPStatusError as exc:
            logger.warning("webhook.failed", document_id=document_id, attempt=attempt,
                           status_code=exc.response.status_code)
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            logger.warning("webhook.failed", document_id=document_id, attempt=attempt, error=str(exc))
        if attempt < _RETRIES:
            await asyncio.sleep(_RETRY_DELAY * attempt)


def fire_and_forget(
    webhook_url: str,
    document_id: str,
    tenant_id: str,
    status: str,
    confidence_score: float | None,
    ocr_engine: str | None,
) -> None:
    """Schedule webhook delivery as a background coroutine — does not block."""
    asyncio.ensure_future(
        dispatch(webhook_url, document_id, tenant_id, status, confidence_score, ocr_engine)
    )
