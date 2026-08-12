"""The webhook SSRF guard.

Every URL here is a literal IP or a bad scheme, so nothing does a real DNS lookup and
these run offline. The one that matters is 169.254.169.254: on EC2 that address serves
the instance's IAM credentials to anything that asks.
"""

import pytest

from nimbus.api.webhooks import is_public_url

BLOCKED = [
    "https://169.254.169.254/latest/meta-data/",  # AWS instance metadata
    "https://127.0.0.1/hook",                     # loopback
    "https://10.0.0.5/hook",                      # private
    "https://192.168.1.1/hook",                   # private
    "https://[::1]/hook",                         # IPv6 loopback
    "http://93.184.216.34/hook",                  # plain http
    "https:///hook",                              # no host
    "ftp://93.184.216.34/hook",                   # not http at all
]

ALLOWED = [
    "https://93.184.216.34/hook",
    "https://8.8.8.8:8443/hook",
]


@pytest.mark.parametrize("url", BLOCKED)
async def test_non_public_urls_are_refused(url):
    assert not await is_public_url(url), f"should have been refused: {url}"


@pytest.mark.parametrize("url", ALLOWED)
async def test_public_https_urls_are_allowed(url):
    assert await is_public_url(url), f"should have been allowed: {url}"
