"""Create a reseller and print its API key. Run this once to bootstrap the system —
without a reseller there is nobody allowed to call the provisioning API.

    cd backend
    uv run python scripts/create_reseller.py "Acme Reseller" [webhook_url]

The key is printed once. Only its hash is stored, so it cannot be shown again.
"""

import asyncio
import sys

from nimbus import db
from nimbus.api import security
from nimbus.models import Reseller


async def main() -> None:
    if len(sys.argv) < 2:
        sys.exit('usage: uv run python scripts/create_reseller.py "Name" [webhook_url]')

    name = sys.argv[1]
    webhook_url = sys.argv[2] if len(sys.argv) > 2 else None

    key, key_hash = security.new_api_key()
    reseller = Reseller(name=name, api_key_hash=key_hash, webhook_url=webhook_url)

    async with db.Session() as session:
        session.add(reseller)
        await session.commit()
        reseller_id = reseller.id

    await db.close()

    print(f"reseller_id : {reseller_id}")
    print(f"api_key     : {key}")
    print()
    print("Save the key now. It is not stored and cannot be recovered.")


asyncio.run(main())
