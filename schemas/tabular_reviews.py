# schemas/tabular_reviews.py
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from datetime import datetime
from enum import Enum

class ReviewScope(str, Enum):
    FILES = "files"
    FOLDER = "folder"

class ReviewStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"

class DataType(str, Enum):
    TEXT = "text"
    NUMBER = "number"
    DATE = "date"
    BOOLEAN = "boolean"
    EMAIL = "email"
    URL = "url"

# Column schemas
class TabularReviewColumnCreate(BaseModel):
    column_name: str = Field(..., min_length=1, max_length=255)
    prompt: str = Field(..., min_length=1, max_length=2000)
    data_type: DataType = Field(default=DataType.TEXT)
    column_order: Optional[int] = None

class TabularReviewColumnResponse(BaseModel):
    id: str
    column_name: str
    prompt: str
    column_order: int
    data_type: str
    created_at: datetime

    class Config:
        from_attributes = True

class TabularReviewColumnUpdate(BaseModel):
    column_name: Optional[str] = Field(None, min_length=1, max_length=255)
    prompt: Optional[str] = Field(None, min_length=1, max_length=2000)
    data_type: Optional[DataType] = None
    column_order: Optional[int] = None

# File schemas
class TabularReviewFileResponse(BaseModel):
    id: str
    file_id: str
    filename: str
    file_size: Optional[int]
    status: str
    added_at: datetime

    class Config:
        from_attributes = True

# Review schemas
class TabularReviewCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=1000)
    columns: List[TabularReviewColumnCreate] = Field(..., min_items=1, max_items=20)
    review_scope: ReviewScope = Field(default=ReviewScope.FILES)
    file_ids: Optional[List[str]] = Field(None, max_items=100)
    folder_id: Optional[str] = None

    class Config:
        use_enum_values = True

class TabularReviewUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=1000)
    status: Optional[ReviewStatus] = None

    class Config:
        use_enum_values = True

class TabularReviewResponse(BaseModel):
    id: str
    user_id: str
    name: str
    description: Optional[str]
    status: str
    review_scope: str
    folder_id: Optional[str]
    created_at: datetime
    updated_at: datetime
    last_processed_at: Optional[datetime]
    columns: List[TabularReviewColumnResponse]
    files: List[TabularReviewFileResponse]
    total_files: int
    total_columns: int
    completion_percentage: float

    class Config:
        from_attributes = True

# Result schemas
class TabularReviewResultResponse(BaseModel):
    id: str
    file_id: str
    column_id: str
    extracted_value: Optional[str]
    confidence_score: Optional[float]
    source_reference: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True

class TabularReviewResultUpdate(BaseModel):
    extracted_value: Optional[str] = None
    confidence_score: Optional[float] = None
    source_reference: Optional[str] = None

# Detailed response with results
class TabularReviewDetailResponse(BaseModel):
    id: str
    user_id: str
    name: str
    description: Optional[str]
    status: str
    review_scope: str
    folder_id: Optional[str]
    created_at: datetime
    updated_at: datetime
    last_processed_at: Optional[datetime]
    columns: List[TabularReviewColumnResponse]
    files: List[TabularReviewFileResponse]
    total_files: int
    total_columns: int
    completion_percentage: float
    results: List[TabularReviewResultResponse]

    class Config:
        from_attributes = True

# List response with pagination
class TabularReviewListResponse(BaseModel):
    reviews: List[TabularReviewResponse]
    total_count: int
    page: int
    page_size: int
    total_pages: int

    class Config:
        from_attributes = True

# Analysis schemas
class AnalysisRequest(BaseModel):
    force_reprocess: bool = False
    specific_columns: Optional[List[str]] = None
    specific_files: Optional[List[str]] = None

class AnalysisStatus(BaseModel):
    review_id: str
    status: str
    progress_percentage: float
    files_processed: int
    total_files: int
    cells_completed: int
    total_cells: int
    estimated_completion: Optional[datetime]
    error_message: Optional[str]
    current_task: Optional[str]

# Gemini integration schemas
class GeminiExtractionRequest(BaseModel):
    document_content: str
    prompts: List[Dict[str, Any]]

class GeminiExtractionResponse(BaseModel):
    extractions: List[Dict[str, Any]]
    processing_time: float
    model_used: str

# Real-time event schemas
class SSEEvent(BaseModel):
    type: str
    review_id: str
    data: Dict[str, Any]
    timestamp: datetime
    user_id: str

# File addition schemas
class AddFilesToReviewRequest(BaseModel):
    file_ids: List[str] = Field(..., min_items=1, max_items=50)

class AddColumnToReviewRequest(BaseModel):
    column_name: str = Field(..., min_length=1, max_length=255)
    prompt: str = Field(..., min_length=1, max_length=2000)
    data_type: DataType = Field(default=DataType.TEXT)

# Export schemas
class ExportFormat(str, Enum):
    CSV = "csv"
    EXCEL = "excel"
    JSON = "json"

class ExportRequest(BaseModel):
    format: ExportFormat = Field(default=ExportFormat.CSV)
    include_metadata: bool = Field(default=True)
    include_confidence: bool = Field(default=True)

# Folder integration schemas
class FolderReviewInfo(BaseModel):
    folder_id: str
    folder_name: str
    folder_color: str
    total_files_in_folder: int
    completed_files_in_folder: int

# Summary schemas
class ReviewSummary(BaseModel):
    total_reviews: int
    active_reviews: int
    completed_reviews: int
    failed_reviews: int
    total_documents_processed: int
    total_extractions: int
    average_confidence: float

# Validation schemas
class BulkValidationRequest(BaseModel):
    updates: List[Dict[str, Any]]  # List of {result_id, extracted_value, is_invalid}

class ValidationStats(BaseModel):
    total_results: int
    validated_results: int
    invalid_results: int
    high_confidence_results: int  # > 0.8
    low_confidence_results: int   # < 0.5
    manually_edited_results: int