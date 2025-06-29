import os
from fastapi import FastAPI, status, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
import json, uuid
from fastapi.encoders import jsonable_encoder
import google.generativeai as genai
import traceback
from contextlib import asynccontextmanager
import asyncio
from api.tabular_review import cleanup_old_buffers, _redis_listener

# ------------ custom JSONResponse to stringify UUIDs ------------
class UUIDEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, uuid.UUID):
            return str(obj)
        return super().default(obj)

class UUIDJSONResponse(JSONResponse):
    def render(self, content: any) -> bytes:
        return json.dumps(
            jsonable_encoder(content),
            cls=UUIDEncoder,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":")
        ).encode("utf-8")

# ------------ single FastAPI instantiation ------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Configure Gemini AI with environment variable
    gemini_api_key = os.getenv("GEMINI_API_KEY")
    if not gemini_api_key:
        print("⚠️  WARNING: GEMINI_API_KEY not found in environment variables")
        raise ValueError("GEMINI_API_KEY environment variable is required")
    
    genai.configure(api_key=gemini_api_key)
    print("✅ Gemini AI configured")
    
    # Start background tasks with error handling
    background_tasks = []
    try:
        bg1 = asyncio.create_task(_redis_listener())
        bg2 = asyncio.create_task(cleanup_old_buffers())
        background_tasks = [bg1, bg2]
        print("✅ Background listeners started")
    except Exception as e:
        print(f"❌ Failed to start background tasks: {e}")
        raise
    
    yield  # Application is running
    
    # Cleanup on shutdown
    print("🔄 Shutting down background tasks...")
    for task in background_tasks:
        task.cancel()
    
    if background_tasks:
        await asyncio.gather(*background_tasks, return_exceptions=True)
    print("✅ Background listeners stopped")


app = FastAPI(
    title="Document Processor API",
    version="1.0.0",
    description="API for document processing with AI-powered reviews",
    default_response_class=UUIDJSONResponse,
    lifespan=lifespan
)


# ------------ Exception handlers for better debugging ------------
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Handle HTTP exceptions (including authentication errors)"""
    print(f"HTTP {exc.status_code} error on {request.method} {request.url}: {exc.detail}")
    
    # Ensure proper headers for authentication errors
    headers = getattr(exc, 'headers', None) or {}
    if exc.status_code == 401 and 'WWW-Authenticate' not in headers:
        headers['WWW-Authenticate'] = 'Bearer'
    
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail,
            "status_code": exc.status_code,
            "type": "http_exception"
        },
        headers=headers
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Handle FastAPI validation errors (422)"""
    print(f"Validation error on {request.method} {request.url}: {exc.errors()}")
    
    # Get more details about the validation error
    error_details = []
    for error in exc.errors():
        error_details.append({
            "field": error.get("loc", [])[-1] if error.get("loc") else "unknown",
            "message": error.get("msg", "Unknown validation error"),
            "type": error.get("type", "unknown"),
            "location": error.get("loc", [])
        })
    
    print(f"Detailed validation errors: {error_details}")
    
    return JSONResponse(
        status_code=422,
        content={
            "detail": "Validation error",
            "errors": error_details,
            "message": "Request validation failed. Please check your input data.",
            "type": "validation_error"
        }
    )

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handle any other exceptions"""
    error_id = str(uuid.uuid4())
    print(f"[{error_id}] Unexpected error on {request.method} {request.url}: {str(exc)}")
    print(f"[{error_id}] Traceback: {traceback.format_exc()}")
    
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error",
            "message": "An unexpected error occurred. Please try again.",
            "error_id": error_id,
            "type": "internal_error"
        }
    )

# ------------ CORS (allow your front-end + SSE preflight) ------------
# Get allowed origins from environment or use defaults
allowed_origins = os.getenv("ALLOWED_ORIGINS", "").split(",") if os.getenv("ALLOWED_ORIGINS") else [
    "http://localhost:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:3001",
    "http://localhost",
    "http://backend:8000",
    "http://backend",
    "http://frontend:3000",
    "http://frontend",
]

# Filter out empty strings
allowed_origins = [origin.strip() for origin in allowed_origins if origin.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=True,
)

# ------------ include your routers ------------
from api import auth, files, health, tabular_review, folder

app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
app.include_router(files.router, prefix="/api/files", tags=["files"])
app.include_router(health.router, prefix="/api/health", tags=["health"])
app.include_router(tabular_review.router, prefix="/api/reviews", tags=["reviews"])
app.include_router(folder.router, prefix="/api/folders", tags=["folders"])

@app.get("/", tags=["root"])
async def root():
    """Root endpoint with basic API information"""
    return {
        "message": "Document Processor API",
        "version": "1.0.0",
        "status": "healthy",
        "docs": "/docs"
    }

# ------------ run ------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app", 
        host="0.0.0.0", 
        port=8000, 
        reload=True,
        log_level="info"
    )