from pydantic import BaseModel, Field


class SpotifyRequest(BaseModel):
    download_id: str = Field(alias="downloadId")
    requested_by: str = Field(alias="requestedBy")
    channel_id: int
    download_url: str = Field(alias="downloadUrl")
    message_id: int
    status: str
    is_admin: bool = False
