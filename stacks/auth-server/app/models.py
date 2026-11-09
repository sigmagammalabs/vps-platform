from typing import Optional

from pydantic import BaseModel, Field


class VerifyRequest(BaseModel):
    license_key: str = Field(min_length=5, max_length=100)
    hwid: str = Field(min_length=10, max_length=100)
    tool_id: str = Field(default="all", pattern=r"^[A-Za-z0-9_-]{1,100}$")
    device_name: Optional[str] = Field(default=None, max_length=150)
    # 16 Zufallsbytes als base64url, vom Client je Anfrage neu erzeugt; wird in
    # das signierte Token uebernommen (Schutz gegen Replay und Fake-Server).
    nonce: str = Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class VerifyResponse(BaseModel):
    valid: bool
    client_name: str = ""
    tool_id: str = ""
    status: str = "active"
    expires_at: Optional[str] = None
    days_left: Optional[int] = None
    token: Optional[str] = None
    message: str = ""
