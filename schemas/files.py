from pydantic import BaseModel
from datetime import datetime
from typing import Optional
from uuid import UUID

class FileResponse(BaseModel):
    id: str
    user_id: str
    original_filename: str
    file_size: Optional[int] = None
    file_type: Optional[str] = None
    storage_path: Optional[str] = None
    storage_url: Optional[str] = None
    status: str
    created_at: datetime
    updated_at: datetime
    processed_at: Optional[datetime] = None
    error_message: Optional[str] = None

    class Config:
        from_attributes = True

class MarkdownResponse(BaseModel):
    id: str
    file_id: str
    user_id: str
    content: str
    word_count: Optional[int] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

class FileUploadRequest(BaseModel):
    """For file upload validation"""
    pass

class FileStatusUpdate(BaseModel):
    """For updating file status"""
    status: str
    processed_at: Optional[datetime] = None
    error_message: Optional[str] = None