import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Database
    database_url: str = os.getenv("DATABASE_URL", "")
    
    # Supabase
    supabase_url: str = os.getenv("SUPABASE_URL", "")
    supabase_key: str = os.getenv("SUPABASE_KEY", "")
    supabase_anon_key: str = os.getenv("SUPABASE_ANON_KEY", "")
    supabase_service_role_key: str = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    
    # Security
    secret_key: str = os.getenv("SECRET_KEY", "your-secret-key")
    jwt_secret: str = os.getenv("JWT_SECRET", "your-jwt-secret-key")
    
    # Redis - Updated for Docker
    redis_url: str = os.getenv("REDIS_URL", "redis://redis:6379")
    celery_broker_url: str = os.getenv("CELERY_BROKER_URL", "redis://redis:6379/0")
    celery_result_backend: str = os.getenv("CELERY_RESULT_BACKEND", "redis://redis:6379/0")
    
    # AI Services
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    
    # API Configuration
    next_public_api_url: str = os.getenv("NEXT_PUBLIC_API_URL", "")
    
    # Storage
    storage_bucket: str = os.getenv("STORAGE_BUCKET", "documents")
    
    # Application Settings
    debug: bool = os.getenv("DEBUG", "false").lower() == "true"
    port: int = int(os.getenv("PORT", "8000"))
    environment: str = os.getenv("ENVIRONMENT", "development")
    
    # Performance Settings
    max_concurrent_extractions: int = int(os.getenv("MAX_CONCURRENT_EXTRACTIONS", "20"))
    max_upload_size_mb: int = int(os.getenv("MAX_UPLOAD_SIZE_MB", "50"))
    chunk_size_tokens: int = int(os.getenv("CHUNK_SIZE_TOKENS", "800"))
    embedding_batch_size: int = int(os.getenv("EMBEDDING_BATCH_SIZE", "20"))
    
    # JWT Settings
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30

    class Config:
        env_file = ".env"

settings = Settings()