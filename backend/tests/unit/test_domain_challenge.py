"""The domain ownership challenge — block L1, HLD §9.6a.

No database, no network. The challenge is a pure function of the domain id and
`JWT_SECRET`, which is the whole reason it needs no column and no migration — and it is
also what makes it testable here rather than only against a live DNS zone.

    uv run pytest tests/unit/test_domain_challenge.py -q
"""

import uuid

import dns.rdata
import dns.rdataclass
import dns.rdatatype
import pytest

from nimbus.api import security

A = uuid.UUID("11111111-1111-1111-1111-111111111111")
B = uuid.UUID("22222222-2222-2222-2222-222222222222")


def test_the_same_domain_always_gets_the_same_challenge():
    """Derived, not stored. A reseller must be able to re-read it days later.

    If this ever stops holding, every domain mid-verification breaks at once and the
    only symptom is a 409 that looks like the reseller's fault.
    """
    assert security.domain_challenge(A) == security.domain_challenge(A)


def test_two_domains_never_share_a_challenge():
    """Otherwise proving control of one domain would prove control of another."""
    assert security.domain_challenge(A) != security.domain_challenge(B)


def test_the_challenge_is_base32_so_case_folding_is_lossless():
    """§9.6a's stated reason for base32 over base64.

    DNS panels re-case what you paste. base32's alphabet is A-Z and 2-7, so upper-casing
    on compare cannot lose information. base64 would use 'a' and 'A' to mean different
    things and the same normalisation would silently corrupt the value.
    """
    token = security.domain_challenge(A)
    assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
    assert "=" not in token, "padding would be dropped by some DNS panels"


def test_the_challenge_fits_in_one_txt_segment():
    """A TXT value over 255 bytes is split on the wire and has to be rejoined.

    The reader joins segments anyway, so this is not load-bearing today — it is here so
    that if someone widens the token (SHA-512, say) the ceiling fails offline instead of
    on a live domain.
    """
    assert len(security.domain_challenge(A)) == 52
    assert len(security.domain_challenge(A)) < 255


@pytest.mark.parametrize(
    "found",
    [
        "AS_IS",
        "lowercased",
        "  surrounded by spaces  ",
        "\tleading tab",
    ],
    ids=["exact", "lowercase", "spaces", "tab"],
)
def test_a_match_survives_what_dns_panels_do_to_it(found):
    """Case and stray whitespace are what actually arrives, not a hypothetical."""
    expected = security.domain_challenge(A)
    mangled = {
        "AS_IS": expected,
        "lowercased": expected.lower(),
        "  surrounded by spaces  ": f"  {expected}  ",
        "\tleading tab": f"\t{expected}",
    }[found]
    assert security.challenge_matches(mangled, expected)


def test_another_domains_challenge_does_not_match():
    assert not security.challenge_matches(security.domain_challenge(B), security.domain_challenge(A))


def test_a_truncated_challenge_does_not_match():
    """The commonest real failure: a panel with a length limit silently clips the value.

    It must fail closed. A prefix comparison would pass here and hand verification to
    anyone who could guess the first few characters.
    """
    expected = security.domain_challenge(A)
    assert not security.challenge_matches(expected[:-1], expected)
    assert not security.challenge_matches(expected[:10], expected)


def test_empty_and_junk_do_not_match():
    expected = security.domain_challenge(A)
    for junk in ("", "   ", "v=spf1 include:_spf.google.com ~all"):
        assert not security.challenge_matches(junk, expected)


@pytest.mark.parametrize(
    "prefix",
    ["�", "“", "é", "–"],
    ids=["replacement-char", "smart-quote", "e-acute", "en-dash"],
)
def test_non_ascii_returns_false_instead_of_raising(prefix):
    """`hmac.compare_digest` REFUSES non-ASCII str — it raises TypeError, not False.

    Two ways that reaches here. A reseller pastes the token out of a rendered doc and
    brings a smart quote with it; or an unrelated TXT record at the challenge host holds
    non-ASCII bytes, which `_lookup_txt` decodes to U+FFFD.

    Either turned `POST /v1/domains/{id}/verify` into a 500, and INTERMITTENTLY: `any()`
    short-circuits, so the same domain passed or 500'd depending on the order DNS
    happened to return its records in. Found by review, reproduced in the venv, fixed at
    the one choke point every value passes through.
    """
    expected = security.domain_challenge(A)
    assert security.challenge_matches(prefix + expected, expected) is False


def test_a_corrupted_record_is_rejected_not_repaired():
    """Non-ASCII fails closed rather than being stripped out and then matched.

    The first fix here sanitised instead, which avoided the 500 but made
    `X<junk>Y<junk>Z` verify against `XYZ` — a genuinely broken DNS record passing while
    the reseller never learns it is broken. No security hole either way (you need the
    token AND the zone), but a 409 naming the problem beats a silent pass.
    """
    expected = security.domain_challenge(A)
    assert not security.challenge_matches(expected[:10] + "é" + expected[11:], expected)
    assert not security.challenge_matches("é".join(expected), expected)
    # The token with a single accented character appended: not our value, so not a match.
    assert not security.challenge_matches(expected + "é", expected)


def test_a_non_breaking_space_is_forgiven():
    """Python's str.strip() treats U+00A0 as whitespace, so this one SHOULD pass.

    Pinned deliberately: it is the difference between "forgiving about copy-paste" and
    "accepts something it should not", and the two are one `.strip()` apart.
    """
    expected = security.domain_challenge(A)
    assert security.challenge_matches(" " + expected + " ", expected)


def test_the_challenge_host_is_a_subdomain_not_the_apex():
    """§9.6a puts the record at `_nimbus-challenge.<domain>` on purpose, so we never
    read — or have an opinion about — the SPF and DMARC records at the root."""
    assert security.CHALLENGE_HOST.startswith("_")
    assert "." not in security.CHALLENGE_HOST


def test_a_long_txt_value_splits_and_rejoins():
    """Pins the dnspython contract the verify endpoint depends on.

    `_lookup_txt` joins `rdata.strings` before comparing. That is only correct if a long
    TXT value really does arrive as several byte segments — assert it against the
    installed library rather than trusting the docs.
    """
    long_value = "X" * 300
    rdata = dns.rdata.from_text(
        dns.rdataclass.IN, dns.rdatatype.TXT, f'"{long_value[:255]}" "{long_value[255:]}"'
    )
    assert len(rdata.strings) == 2, "a >255 byte value must arrive in segments"
    assert all(isinstance(s, bytes) for s in rdata.strings)
    assert b"".join(rdata.strings).decode() == long_value


def test_a_short_txt_value_is_one_segment():
    rdata = dns.rdata.from_text(
        dns.rdataclass.IN, dns.rdatatype.TXT, f'"{security.domain_challenge(A)}"'
    )
    assert len(rdata.strings) == 1
    assert b"".join(rdata.strings).decode() == security.domain_challenge(A)
