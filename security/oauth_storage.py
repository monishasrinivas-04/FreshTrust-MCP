
"""Encrypted persistent OAuth storage for the GitHub MCP connection."""

from datetime import datetime, timezone

from sqlalchemy import (
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    select,
    func,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

from mcp.client.auth.oauth2 import TokenStorage
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthToken,
)

from config.settings import settings
from security.encryption import encrypt_data, decrypt_data


metadata = MetaData()

oauth_credentials = Table(
    "github_oauth_credentials",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("tokens_encrypted", Text, nullable=True),
    Column("client_info_encrypted", Text, nullable=True),
    Column(
        "updated_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)

STORAGE_ID = "administrator"

TOKENS_AAD = b"FreshTrust-MCP:github-oauth:tokens:v1"
CLIENT_INFO_AAD = b"FreshTrust-MCP:github-oauth:client-info:v1"


class NeonOAuthTokenStorage(TokenStorage):
    """Store MCP OAuth credentials encrypted in Neon PostgreSQL."""

    def __init__(self):
        if not settings.database_url:
            raise RuntimeError(
                "DATABASE_URL is required for persistent OAuth storage."
            )

        if not settings.github_oauth_encryption_key:
            raise RuntimeError(
                "GITHUB_OAUTH_ENCRYPTION_KEY is not configured."
            )

        if not settings.github_oauth_client_id:
            raise RuntimeError(
                "GITHUB_OAUTH_CLIENT_ID is not configured."
            )

        if not settings.github_oauth_client_secret:
            raise RuntimeError(
                "GITHUB_OAUTH_CLIENT_SECRET is not configured."
            )

        if not settings.github_oauth_redirect_uri:
            raise RuntimeError(
                "GITHUB_OAUTH_REDIRECT_URI is not configured."
            )

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

        self.engine = create_engine(
            db_url,
            pool_pre_ping=True,
            pool_recycle=300,
        )

        self.key = settings.github_oauth_encryption_key

        metadata.create_all(self.engine)

    def _encrypt(self, value, associated_data):
        return encrypt_data(
            value,
            self.key,
            associated_data=associated_data,
        )

    def _decrypt(self, value, associated_data):
        return decrypt_data(
            value,
            self.key,
            associated_data=associated_data,
        )

    def _upsert(self, **values):
        values["id"] = STORAGE_ID
        values["updated_at"] = datetime.now(timezone.utc)

        statement = pg_insert(oauth_credentials).values(**values)

        update_values = {
            key: getattr(statement.excluded, key)
            for key in values
            if key != "id"
        }

        statement = statement.on_conflict_do_update(
            index_elements=["id"],
            set_=update_values,
        )

        with self.engine.begin() as connection:
            connection.execute(statement)

    async def get_tokens(self) -> OAuthToken | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(oauth_credentials.c.tokens_encrypted).where(
                    oauth_credentials.c.id == STORAGE_ID
                )
            ).first()

        if row is None or not row.tokens_encrypted:
            return None

        payload = self._decrypt(
            row.tokens_encrypted,
            TOKENS_AAD,
        )

        return OAuthToken.model_validate_json(payload)

    async def set_tokens(self, tokens: OAuthToken) -> None:
        if not isinstance(tokens, OAuthToken):
            raise TypeError("Expected an OAuthToken instance.")

        encrypted = self._encrypt(
            tokens.model_dump_json(),
            TOKENS_AAD,
        )

        self._upsert(tokens_encrypted=encrypted)

    async def get_client_info(
        self,
    ) -> OAuthClientInformationFull | None:

        with self.engine.connect() as connection:
            row = connection.execute(
                select(oauth_credentials.c.client_info_encrypted).where(
                    oauth_credentials.c.id == STORAGE_ID
                )
            ).first()

        if row is not None and row.client_info_encrypted:
            payload = self._decrypt(
                row.client_info_encrypted,
                CLIENT_INFO_AAD,
            )

            return OAuthClientInformationFull.model_validate_json(
                payload
            )

        # Use the GitHub App credentials registered by the administrator.
        # This avoids depending on unsupported dynamic client registration.
        return OAuthClientInformationFull(
            client_id=settings.github_oauth_client_id,
            client_secret=settings.github_oauth_client_secret,
            redirect_uris=[
                settings.github_oauth_redirect_uri
            ],
            token_endpoint_auth_method="client_secret_post",
            grant_types=[
                "authorization_code",
                "refresh_token",
            ],
            response_types=["code"],
        )

    async def set_client_info(
        self,
        client_info: OAuthClientInformationFull,
    ) -> None:

        if not isinstance(
            client_info,
            OAuthClientInformationFull,
        ):
            raise TypeError(
                "Expected an OAuthClientInformationFull instance."
            )

        encrypted = self._encrypt(
            client_info.model_dump_json(),
            CLIENT_INFO_AAD,
        )

        self._upsert(client_info_encrypted=encrypted)
