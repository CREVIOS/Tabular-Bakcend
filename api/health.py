from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from datetime import datetime
from core.supabase_create import get_supabase_admin

router = APIRouter()

class HealthResponse(BaseModel):
    status: str
    timestamp: datetime
    database: str
    storage: str

@router.get("/", response_model=HealthResponse)
async def health_check():
    """
    Health check endpoint to verify API and database connectivity
    """
    try:
        # Test database connection
        supabase_admin = get_supabase_admin()
        
        # Simple query to test database connectivity
        db_response = supabase_admin.table("users").select("id").limit(1).execute()
        database_status = "healthy" if not (hasattr(db_response, 'error') and db_response.error) else "unhealthy"
        
        # Test storage connection (simple check)
        try:
            storage_response = supabase_admin.storage.list_buckets()
            storage_status = "healthy" if storage_response else "unhealthy"
        except Exception:
            storage_status = "unhealthy"
        
        overall_status = "healthy" if database_status == "healthy" and storage_status == "healthy" else "unhealthy"
        
        return HealthResponse(
            status=overall_status,
            timestamp=datetime.utcnow(),
            database=database_status,
            storage=storage_status
        )
        
    except Exception as e:
        return HealthResponse(
            status="unhealthy",
            timestamp=datetime.utcnow(),
            database="unhealthy",
            storage="unhealthy"
        )