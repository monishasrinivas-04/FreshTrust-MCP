"""SQLAlchemy cache supporting local SQLite and hosted PostgreSQL."""

import hashlib
import time
from pathlib import Path

from sqlalchemy import (
    Column,
    Float,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    inspect,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from config.settings import settings


metadata = MetaData()

files = Table(
    "files",
    metadata,
    Column("cache_key", String(64), primary_key=True),
    Column("repo", Text, nullable=False),
    Column("path", Text, nullable=False),
    Column("content", Text, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("commit_sha", Text),
    Column("fetched_epoch", Float, nullable=False),
    Column("revision", Text),
    Column("commit_timestamp", Text),
)


class FileCache:
    def __init__(self, path=None):
        if path is not None:
            db_url = f"sqlite:///{Path(path).resolve().as_posix()}"
        elif settings.database_url:
            db_url = settings.database_url.strip()

            if db_url.startswith("postgres://"):
                db_url = db_url.replace(
                    "postgres://",
                    "postgresql+psycopg://",
                    1,
                )
            elif db_url.startswith("postgresql://"):
                db_url = db_url.replace(
                    "postgresql://",
                    "postgresql+psycopg://",
                    1,
                )
        else:
            local_path = Path(settings.cache_path)
            local_path.parent.mkdir(parents=True, exist_ok=True)
            db_url = f"sqlite:///{local_path.resolve().as_posix()}"

        engine_options = {"pool_pre_ping": True}

        if db_url.startswith("sqlite:"):
            engine_options["connect_args"] = {"check_same_thread": False}
        else:
            engine_options["pool_recycle"] = 300

        self.engine = create_engine(db_url, **engine_options)

        metadata.create_all(self.engine)
        self._migrate_schema()

    def _migrate_schema(self):
        """Add columns required by newer cache versions."""
        existing_columns = {
            column["name"]
            for column in inspect(self.engine).get_columns("files")
        }

        migrations = {
            "revision": "TEXT",
            "commit_timestamp": "TEXT",
        }

        with self.engine.begin() as connection:
            for column_name, column_type in migrations.items():
                if column_name not in existing_columns:
                    connection.exec_driver_sql(
                        f"ALTER TABLE files ADD COLUMN "
                        f"{column_name} {column_type}"
                    )

    @staticmethod
    def _key(repo, path, revision):
        value = f"{repo}:{path}:{revision or 'unknown'}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def get(self, repo, path, revision=None, allow_expired=False):
        key = self._key(repo, path, revision)

        with self.engine.connect() as connection:
            row = connection.execute(
                select(
                    files.c.content,
                    files.c.content_hash,
                    files.c.commit_sha,
                    files.c.fetched_epoch,
                    files.c.revision,
                    files.c.commit_timestamp,
                ).where(files.c.cache_key == key)
            ).mappings().first()

        if row is None:
            return None

        age = time.time() - row["fetched_epoch"]

        if age < 0 or (
            not allow_expired
            and age > settings.cache_ttl_seconds
        ):
            return None

        return {
            "path": path,
            "content": row["content"],
            "content_hash": row["content_hash"],
            "commit_sha": row["commit_sha"],
            "fetched_epoch": row["fetched_epoch"],
            "revision": row["revision"],
            "commit_timestamp": row["commit_timestamp"],
            "cache_age_seconds": age,
        }

    def list_repo(self, repo):
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    files.c.path,
                    files.c.content,
                    files.c.content_hash,
                    files.c.commit_sha,
                    files.c.fetched_epoch,
                    files.c.revision,
                    files.c.commit_timestamp,
                ).where(files.c.repo == repo)
            ).mappings().all()

        latest = {}

        for row in rows:
            item = dict(row)

            if (
                item["path"] not in latest
                or item["fetched_epoch"]
                > latest[item["path"]]["fetched_epoch"]
            ):
                latest[item["path"]] = item

        return list(latest.values())

    def put(
        self,
        repo,
        path,
        content,
        revision=None,
        commit_sha=None,
        commit_timestamp=None,
    ):
        digest = hashlib.sha256(
            content.encode("utf-8")
        ).hexdigest()

        key = self._key(repo, path, revision)

        values = {
            "cache_key": key,
            "repo": repo,
            "path": path,
            "content": content,
            "content_hash": digest,
            "commit_sha": commit_sha,
            "fetched_epoch": time.time(),
            "revision": revision,
            "commit_timestamp": commit_timestamp,
        }

        dialect = self.engine.dialect.name

        if dialect == "postgresql":
            statement = pg_insert(files).values(**values)
            statement = statement.on_conflict_do_update(
                index_elements=["cache_key"],
                set_={
                    column: getattr(statement.excluded, column)
                    for column in values
                    if column != "cache_key"
                },
            )
        elif dialect == "sqlite":
            statement = sqlite_insert(files).values(**values)
            statement = statement.on_conflict_do_update(
                index_elements=["cache_key"],
                set_={
                    column: getattr(statement.excluded, column)
                    for column in values
                    if column != "cache_key"
                },
            )
        else:
            raise RuntimeError(
                f"Unsupported cache database: {dialect}"
            )

        with self.engine.begin() as connection:
            connection.execute(statement)

        return digest