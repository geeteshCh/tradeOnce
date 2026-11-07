"""MongoDB connection settings loaded from the process environment."""

import os

from pydantic import BaseModel, Field, SecretStr


class MongoSettings(BaseModel):
    """Connection configuration; the URI is masked in model representations."""

    uri: SecretStr = Field(min_length=1)
    database: str = Field(default="tradeonce", min_length=1)

    @classmethod
    def from_env(cls) -> "MongoSettings":
        """Read MONGODB_URI and optional MONGODB_DATABASE, then validate them."""
        return cls.model_validate(
            {
                "uri": os.environ.get("MONGODB_URI"),
                "database": os.environ.get("MONGODB_DATABASE", "tradeonce"),
            }
        )
