from typing import Optional, List
from pydantic import BaseModel, Field


class VerifyRequest(BaseModel):
    license_key: str = Field(min_length=5, max_length=100)
    hwid: str = Field(min_length=10, max_length=100)
    tool_id: str = Field(default="all", max_length=100)
    device_name: Optional[str] = Field(default=None, max_length=150)


class VerifyResponse(BaseModel):
    valid: bool
    client_name: str = ""
    tool_id: str = ""
    status: str = "active"
    expires_at: Optional[str] = None
    days_left: Optional[int] = None
    token: Optional[str] = None
    message: str = ""


class CreateLicenseRequest(BaseModel):
    client_name: str = Field(min_length=2, max_length=150)
    tool_id: str = Field(default="all", max_length=100)
    days_valid: Optional[int] = Field(default=30)
    max_devices: int = Field(default=1, ge=1, le=10)
    notes: Optional[str] = Field(default="", max_length=500)


class DeviceInfo(BaseModel):
    hwid: str
    device_name: Optional[str] = None
    first_seen_at: str
    last_seen_at: str
    ip_address: Optional[str] = None


class LicenseInfo(BaseModel):
    id: int
    license_key: str
    client_name: str
    tool_id: str
    max_devices: int
    status: str
    expires_at: Optional[str] = None
    days_left: Optional[int] = None
    notes: Optional[str] = None
    created_at: str
    updated_at: str
    devices: List[DeviceInfo] = []
