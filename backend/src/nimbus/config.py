"""Settings, read from `backend/.env` and validated on startup.

Nothing here has a working default. That is the point: the previous version fell back
to `postgresql://nimbus:nimbus@localhost:5433/nimbus` and a fixed JWT secret, so real
credentials lived in source control and a deploy that forgot to set them still booted —
quietly, with a signing key anyone reading the repo already knew.

Now a missing or short value raises before the first request is served. Setup is one
command:

    cp backend/.env.example backend/.env

The file is resolved from this module's own path, not the working directory, because
the API, the worker, Alembic and pytest are all started from different places.
"""

from pathlib import Path

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

# src/nimbus/config.py -> src/nimbus -> src -> backend
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        # Real environment variables win over the file, which is how production is
        # configured: no .env on the box, everything injected by the platform.
        extra="ignore",
    )

    # Plain `postgresql://` here, with no driver. The driver belongs to whoever is
    # connecting — the app uses asyncpg, Alembic uses psycopg — and the two are derived
    # below rather than written twice in .env where they could drift apart.
    database_url: str
    redis_url: str

    # HS256 signs every mailbox token with this. Guessing it forges a token for ANY
    # mailbox in ANY tenant, which is all of HLD §10.1 isolation. RFC 7518 §3.2 sets the
    # floor at the hash width — 32 bytes — and PyJWT warns below it.
    jwt_secret: str = Field(min_length=32)
    jwt_ttl_hours: int = 24

    # The receiver's own settings live in its Go environment, not here. This one is
    # duplicated only because create_reseller.py prints it in setup instructions.
    api_base_url: str = "http://127.0.0.1:8000"

    # ---- Object storage and the event bus, used by the worker (blocks C, D, G) ----
    #
    # Credentials and addresses are required, following the same rule as the database:
    # a wrong default here fails loudly, a defaulted secret fails silently. Bucket and
    # topic names DO get defaults, because a wrong one raises "no such bucket" on the
    # first call — obvious in seconds, and it saves four lines of .env that never change.
    s3_endpoint: str          # http://localhost:9000 locally; unset/AWS in production
    s3_access_key: str
    s3_secret_key: str
    s3_region: str = "us-east-1"
    s3_raw_bucket: str = "nimbus-raw"      # whole .eml files, written by the receiver
    s3_chunk_bucket: str = "nimbus-chunks"  # deduplicated attachment chunks

    kafka_seeds: str          # comma-separated, matches the Go receiver's KAFKA_SEEDS
    kafka_topic: str = "mail.received"
    # The consumer group. Every worker sharing this name splits the partitions between
    # them; a NEW name would replay the whole topic from the beginning.
    kafka_group_id: str = "nimbus-worker"

    @computed_field
    @property
    def sqlalchemy_url(self) -> str:
        """Async, for the application."""
        return self.database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    @computed_field
    @property
    def alembic_url(self) -> str:
        """Sync, for migrations.

        asyncpg wraps statements in prepared statements, and Postgres rejects those when
        the string holds more than one command — that would force one `op.execute()` per
        statement forever. Migrations are one-shot admin work, so sync is right there.
        """
        return self.database_url.replace("postgresql://", "postgresql+psycopg://", 1)


settings = Settings()
