"""Alembic environment.

Runs on the SYNC psycopg driver, not asyncpg. Migrations are a one-shot admin task on
a single connection, so async gains nothing — and asyncpg wraps every statement in a
prepared statement, which Postgres refuses to accept with multiple commands in it.
That would force every migration to be split into one call per statement. The app
itself still uses asyncpg; only this tool is different.
"""

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# The Alembic Config object, giving access to values in alembic.ini.
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ---------------------------------------------------------------------------
# The connection string comes from nimbus.config, never from alembic.ini.
# Two copies of a database URL is how you migrate the wrong database.
#
# `settings.alembic_url` names the sync psycopg driver, which SQLAlchemy requires and
# the asyncpg-based application code does not.
# ---------------------------------------------------------------------------
from nimbus.config import settings
from nimbus.models import Base

config.set_main_option("sqlalchemy.url", settings.alembic_url)

# What `--autogenerate` diffs the live database against.
#
# Two things it cannot see, which is why the initial migration stays hand-written and
# must never be regenerated from these models:
#
#   1. The refcount TRIGGER on mailbox_message. Alembic does not model triggers at all.
#      It is the only place blob.refcount and mailbox.used_bytes go down, so losing it
#      would stop GC freeing anything — silently.
#   2. The pgcrypto EXTENSION that gen_random_uuid() comes from.
#
# So: models follow the schema, they do not define it. A run of
# `alembic revision --autogenerate` that produces anything means the two have drifted.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit the SQL to stdout instead of running it.

    Useful for handing a DBA the exact statements, or reviewing before production.
        uv run alembic upgrade head --sql
    """
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and apply the migrations."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # one connection, then gone. No pool needed.
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
