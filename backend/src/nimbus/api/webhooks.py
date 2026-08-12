"""Calling a reseller's webhook — and refusing to call our own network.

Not a router: nothing here serves a request. It is an outbound HTTP client that
provisioning hands work to in the background.
"""

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx

log = logging.getLogger("nimbus.api.webhooks")


async def is_public_url(url: str) -> bool:
    """Reject anything that would make us fetch our own network.

    A webhook URL is a stored string that this server then requests. Point it at
    https://169.254.169.254/ on EC2 and our reply is the instance's IAM credentials —
    that is SSRF (Server-Side Request Forgery: making the server fetch something on the
    attacker's behalf, from inside a network they cannot reach).

    `ip_address().is_global` is False for private, loopback, link-local, multicast and
    reserved ranges, so one stdlib property covers every case worth naming.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    try:
        # The event-loop resolver, not socket.getaddrinfo — that one blocks, and a
        # single slow DNS answer would stall every other request on this process.
        infos = await asyncio.get_running_loop().getaddrinfo(
            parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM
        )
    except socket.gaierror:
        return False
    # ponytail: checks the address we resolve, not the one httpx finally connects to.
    # A DNS record that flips between the two calls slips through. Close it with a
    # pinned-IP transport if webhook URLs ever become self-service.
    return all(ipaddress.ip_address(info[4][0]).is_global for info in infos)


async def call(url: str, body: dict) -> None:
    """Three tries with a widening gap, then give up and log it.

    The reseller's endpoint being down must never fail a provisioning call that has
    already committed. The mailboxes exist either way; only the notification is lost.
    """
    if not await is_public_url(url):
        log.warning("refusing webhook to non-public URL: %s", url)
        return

    async with httpx.AsyncClient(timeout=10) as client:
        for delay in (0, 2, 8):
            if delay:
                await asyncio.sleep(delay)
            try:
                response = await client.post(url, json=body)
                if response.status_code < 400:
                    return
            except httpx.HTTPError:
                pass
    # ponytail: fire-and-forget with 3 retries. If resellers start needing guaranteed
    # delivery, move this to a table the GC worker sweeps, not a bigger retry loop.
    log.warning("webhook failed after 3 attempts: %s", url)
