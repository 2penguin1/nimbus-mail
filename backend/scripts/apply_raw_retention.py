"""Expire raw `.eml` objects after RETENTION_DAYS. Run once per environment.

    uv run python scripts/apply_raw_retention.py          # apply
    uv run python scripts/apply_raw_retention.py --show    # read the rule back

**Why this exists.** The receiver stores every message whole in `nimbus-raw`, and the
worker reads it exactly once, during processing (`storage.open_raw_message`, called only
from `pipeline._read_and_split`). Nothing reads it ever again. Measured on the
development stack: `nimbus-raw` held **2004 MB** against the chunk store's **310 MB** —
6.5x the bytes that garbage collection was built to reclaim.

That gap is not a rounding error, it is the storage story. Every attachment is held
twice: base64-encoded inside the raw message, and deduplicated in binary in the chunk
store. And the raw copy dedups across nothing — forty separate emails carrying the same
deck write forty full copies, because each is a distinct message with its own key. So
"50% saved" would describe 13% of what is actually on disk.

**Why a lifecycle rule and not GC.** Deleting the raw object in GC phase 1 is more
precise — the raw copy would live exactly as long as the mail does — but two resellers
receiving one email share a single `raw_s3_key`, so it needs an "is anyone else still
using this key" check and a new index on `message(raw_s3_key)`. That is more code in the
one block where a mistake destroys customer data. The lifecycle rule needs neither: the
object store expires objects by age on its own, and seven days is a recovery window
rather than a correctness boundary.

**What we give up.** After seven days there is no original message any more, so there can
be no "download the original .eml" feature for older mail, and mail cannot be replayed
through the worker to rebuild anything. Everything the product actually serves —
headers, body, attachments, search — comes from Postgres and the chunk store, both of
which are permanent. HLD §11 records the trade.
"""

import argparse
import json

from nimbus.config import settings
from nimbus.storage import client as s3

RETENTION_DAYS = 7
RULE_ID = "expire-raw-messages"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show", action="store_true", help="print the current rule and exit")
    args = parser.parse_args()

    if args.show:
        try:
            current = s3.get_bucket_lifecycle_configuration(Bucket=settings.s3_raw_bucket)
            print(json.dumps(current["Rules"], indent=2, default=str))
        except s3.exceptions.ClientError as error:
            print(f"no lifecycle rule on {settings.s3_raw_bucket}: {error}")
        return

    # Empty prefix: every object in the bucket. The raw bucket holds nothing but raw
    # messages, so there is nothing here worth keeping longer.
    s3.put_bucket_lifecycle_configuration(
        Bucket=settings.s3_raw_bucket,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": RULE_ID,
                    "Status": "Enabled",
                    "Filter": {"Prefix": ""},
                    "Expiration": {"Days": RETENTION_DAYS},
                }
            ]
        },
    )
    print(
        f"{settings.s3_raw_bucket}: raw messages now expire after {RETENTION_DAYS} days.\n"
        "The chunk store is unaffected — it is permanent, and it is what the API serves."
    )


if __name__ == "__main__":
    main()
