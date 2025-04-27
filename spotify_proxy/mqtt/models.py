from pydantic import BaseModel


class SpotifyRequest(BaseModel):
    download_id: str
    requested_by: str
    request_chat_id: int
    download_url: str
    request_message_id: int
    status: str
    is_admin: bool = False
