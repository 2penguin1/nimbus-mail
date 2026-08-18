"""The load-test corpus is pure, so its published numbers are checkable offline.

This is what stops HLD §13's headline from being arithmetic in a document. The ratio
block J publishes is predicted here, from the plan alone, with no stack running. If
someone changes the mix, these fail and the doc has to be updated deliberately rather
than drifting away from the code.

    uv run pytest tests/unit/test_loadtest_corpus.py -q
"""

import importlib.util
import pathlib

import pytest

# scripts/ is operator tooling, not part of the installed `nimbus` package, so it is
# loaded by path. The alternative — moving the generator into the package — would put
# a load-test-only concern in the shipped code to save three lines here.
_PATH = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "loadtest.py"
_spec = importlib.util.spec_from_file_location("loadtest", _PATH)
loadtest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(loadtest)


N = 10_000
SEED = 42


@pytest.fixture(scope="module")
def plans():
    return loadtest.build_corpus(SEED, N)


def test_the_mix_is_what_the_docs_claim(plans):
    """70 / 24 / 6 per hundred, and the pool is 120 files at n=10,000."""
    assert len(plans) == N
    attached = [p for p in plans if p.attachment]
    assert len(attached) == 3000, "30% attachment rate"

    seeds = {p.attachment.seed for p in attached}
    assert len(seeds) == 720, "120 pool + 600 unique"
    assert loadtest.pool_size(N) == 120

    # 2,400 shared sends across 120 files, 600 one-off files sent once each.
    counts = {}
    for p in attached:
        counts[p.attachment.seed] = counts.get(p.attachment.seed, 0) + 1
    sent_once = sum(1 for c in counts.values() if c == 1)
    assert sent_once >= 600, "the one-off files must actually be one-off"
    assert max(counts.values()) <= loadtest.FANOUT_CAP


def test_the_published_ratios_do_not_drift(plans):
    """Seed 42's numbers, as published in HLD §13. Change the mix, update the doc.

    Tight bounds on purpose. These are not "roughly right" figures — they are the
    prediction the live run is checked against, so a silent change to the mix must
    fail here rather than quietly move the headline.
    """
    e = loadtest.expected(plans)
    assert e["copies"] == 39_497
    assert e["distinct_files"] == 720
    assert 0.687 < e["r1"] < 0.689, f"R1 drifted to {e['r1']:.4f}"
    assert 0.919 < e["r2"] < 0.921, f"R2 drifted to {e['r2']:.4f}"
    assert e["r1"] > 0.60, "§13's floor"


def test_the_ratio_swings_by_seed_and_that_is_the_point(plans):
    """R1 moves 12+ points across five seeds. §13 says this is a corpus property.

    Asserted rather than merely observed, because a future change that accidentally
    removed the variance — pinning every attachment to one size, say — would make the
    number look more trustworthy while being less honest. If this ever fails because
    the spread narrowed, check WHY before relaxing it.
    """
    r1 = [loadtest.expected(loadtest.build_corpus(s, N))["r1"] for s in range(42, 47)]
    assert max(r1) - min(r1) > 0.05, "the spread is the finding; do not tune it away"
    assert all(x > 0.60 for x in r1), "every seed must still clear §13's floor"


def test_no_message_delivers_to_the_same_mailbox_twice(plans):
    """`blob.refcount` moves by rows written, so a duplicate recipient would make the
    expected copy count wrong and the integrity assertion useless."""
    for p in plans:
        assert len(set(p.recipients)) == len(p.recipients)
        assert p.recipients, "every message needs at least one recipient"
        assert max(p.recipients) < loadtest.MAILBOXES


def test_the_same_seed_gives_byte_identical_mail():
    """The whole reason the corpus needs no disk: --seed IS the corpus."""
    a = loadtest.build_corpus(7, 200)
    b = loadtest.build_corpus(7, 200)
    assert [p.subject for p in a] == [p.subject for p in b]
    assert [p.recipients for p in a] == [p.recipients for p in b]

    spec = next(p.attachment for p in a if p.attachment)
    assert spec.content() == spec.content()
    assert len(spec.content()) == spec.size


def test_different_seeds_generate_different_bytes():
    """Two runs must not accidentally dedup against each other's files.

    Blobs are scoped per reseller so a collision could not corrupt anything, but it
    would make the second run's ratio a lie about the first run's data.
    """
    a = next(p.attachment for p in loadtest.build_corpus(1, 200) if p.attachment)
    b = next(p.attachment for p in loadtest.build_corpus(2, 200) if p.attachment)
    assert a.seed != b.seed
    # Different seeds is the mechanism; different BYTES is the claim. Asserting only
    # the seeds would keep passing if `content()` ever stopped using the seed.
    assert a.content()[:4096] != b.content()[:4096]


def test_attachments_stay_under_the_receivers_limit(plans):
    """MAX_MESSAGE_MB is 40 and base64 expands binary by ~1.37x. A 25 MB attachment
    encodes to ~34 MB, which fits — but only just, so the ceiling is asserted."""
    biggest = max(p.attachment.size for p in plans if p.attachment)
    assert biggest <= 25 * 1024 * 1024
    assert biggest * 1.37 < 40 * 1024 * 1024, "would be rejected 552 by the receiver"


def test_fanout_allocation_is_exact():
    """The Zipf split must hand out exactly the sends asked for, or the predicted
    ratio is computed from a corpus that was never sent."""
    for k, total in ((120, 2400), (10, 10), (379, 24000), (50, 5000)):
        counts = loadtest._fanout(k, total)
        assert len(counts) == k
        assert sum(counts) == total
        assert min(counts) >= 1
        assert max(counts) <= loadtest.FANOUT_CAP
