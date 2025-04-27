"""Configuration module"""

import structlog
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = structlog.get_logger().bind(component="Config")


class MQTTConfig(BaseSettings):
    """MQTT configuration class"""

    model_config = SettingsConfigDict(
        env_prefix="GX_MQTT_",
        env_file=".env",
        extra="allow",
        case_sensitive=False,
    )

    host: str = Field(default="localhost")
    port: int = Field(default=1883)
    username: str = Field(default="")
    password: str = Field(default="")
    client_id: str = Field(default="spotify-proxy")
    keepalive: int = Field(default=60)


class MQTTTopics(BaseSettings):
    """MQTT topics class"""

    model_config = SettingsConfigDict(
        env_prefix="GX_MQTT_",
        env_file=".env",
        extra="allow",
        case_sensitive=False,
    )

    topic_prefix: str = Field(default="/gx-lab/spotify")

    @property
    def download_request_topic(self) -> str:
        """Topic for download requests"""
        return f"{self.topic_prefix}/download"

    @property
    def download_status_topic(self) -> str:
        """Topic for download status updates"""
        return f"{self.topic_prefix}/download/status"


class SpotifyConfig(BaseSettings):
    """Spotify configuration class"""

    model_config = SettingsConfigDict(
        env_prefix="GX_SPOTIFY_",
        env_file=".env",
        extra="allow",
        case_sensitive=False,
    )

    destination_folder: str = Field(default="music")
    download_path: str = Field(default="downloads")

    client_id: str | None = Field(default=None)
    client_secret: str | None = Field(default=None)

    ffmpeg_path: str = Field(default="/usr/bin/ffmpeg")

    format: str = Field(default="flac")
    quality: str = Field(default="high")

    download_lyrics: bool = Field(default=False)
    download_metadata: bool = Field(default=False)
    download_cover: bool = Field(default=False)

    max_concurrent_downloads: int = Field(default=3)
    slot_monitor_interval_seconds: int = Field(default=10)


class GXConfig(BaseSettings):
    """Configuration class"""

    model_config = SettingsConfigDict(
        env_prefix="GX_",
        env_file=".env",
        extra="allow",
        case_sensitive=False,
    )

    debug: bool = Field(default=False)
    log_level: str = Field(default="INFO")

    mqtt: MQTTConfig = Field(default_factory=MQTTConfig)
    mqtt_topics: MQTTTopics = Field(default_factory=MQTTTopics)
    spotify: SpotifyConfig = Field(default_factory=SpotifyConfig)
