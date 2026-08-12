"""The processing worker — consumes `mail.received` and runs block C.

    cd backend
    uv run python -m nimbus.worker.main

One message at a time, deliberately. Throughput comes from running several of these:
they share a Kafka consumer group, so Redpanda splits the topic's 4 partitions between
them and no two workers ever see the same message.
"""

import asyncio
import json
import logging

from aiokafka import AIOKafkaConsumer
from nimbus import db
from nimbus.config import settings
from nimbus.worker import pipeline

log = logging.getLogger("nimbus.worker")



async def run() -> None:
    consumer = AIOKafkaConsumer(
        settings.kafka_topic,
        bootstrap_servers=settings.kafka_seeds.split(","),
        group_id=settings.kafka_group_id,
        # Manual commit, and this is the whole reliability story. With auto-commit, the
        # offset can advance on a timer while a message is still being processed — crash
        # in that window and the mail is acked but never stored. Gone, with no error.
        enable_auto_commit=False,
        # A brand-new group reads the topic from the start, so mail that arrived while
        # no worker was running still gets processed.
        auto_offset_reset="earliest",
    )
    await consumer.start()
    log.info(
        "consuming %s from %s as group %s",
        settings.kafka_topic, settings.kafka_seeds, settings.kafka_group_id,
    )
    try:
        async for record in consumer:
            await _handle(record)
            # Only now. Everything above is committed and durable.
            await consumer.commit()
    finally:
        await consumer.stop()
        await db.close()


# Most failures here are transient and clear by themselves: a Postgres deadlock between
# two workers storing blobs that share chunks, an S3 timeout, a broker blip. Retrying the
# same event fixes all of those, because processing is idempotent — the processed_event
# guard means a retry after a rollback starts from a clean slate.
RETRY_DELAYS = (2, 8, 30)


async def _handle(record) -> None:
    """Process one record, retrying transient failures before giving up.

    Giving up means raising, which exits the process. That is deliberate and it is the
    safe direction: the Kafka offset is never committed, so nothing is lost and the mail
    is redelivered when the worker comes back. Skipping the message instead would lose
    somebody's email silently, which is the one outcome worth stopping over.

    """
    # ponytail: no dead-letter topic. A genuinely poison message stops this worker until
    # a human looks at it. Correct, not convenient. Ceiling: one bad message halts one
    # partition. Upgrade path: a dead-letter topic in block K, if malformed mail turns
    # out to be common rather than theoretical.
    for attempt, delay in enumerate((*RETRY_DELAYS, None), start=1):
        try:
            # Inside the try: a malformed event body must be logged like any other
            # failure, not vanish as an unhandled JSONDecodeError.
            event = json.loads(record.value)
            async with db.Session() as session:
                await pipeline.process_event(session, event)
                await session.commit()
            return
        except Exception:
            if delay is None:
                log.exception("giving up after %d attempts, not acking", attempt)
                raise
            log.warning(
                "attempt %d failed, retrying in %ds", attempt, delay, exc_info=True
            )
            await asyncio.sleep(delay)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("shutting down")
