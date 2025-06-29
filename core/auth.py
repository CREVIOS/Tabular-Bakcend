# core/auth.py (Updated version)
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from fastapi.security import HTTPBearer
from jose import JWTError, jwt
from core.supabase_create import get_supabase_client
from core.config import settings
import logging
from supabase import Client, create_client
from supabase.lib.client_options import ClientOptions

security = HTTPBearer()

# Configure logging
logger = logging.getLogger(__name__)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.secret_key, algorithm=settings.algorithm)
    return encoded_jwt

# def verify_token(token: str):
#     try:

#         payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
#         user_id: str = payload.get("sub")
#         if user_id is None:
#             print("No user_id in token payload")
#             raise HTTPException(
#                 status_code=status.HTTP_401_UNAUTHORIZED,
#                 detail="Could not validate credentials",
#             )
#         print(f"Token verified successfully for user_id: {user_id}")
#         return user_id
#     except JWTError as e:
#         print(f"JWT Error: {str(e)}")
#         raise HTTPException(
#             status_code=status.HTTP_401_UNAUTHORIZED,
#             detail="Could not validate credentials",
#         )
#     except Exception as e:
#         print(f"Token verification error: {str(e)}")
#         raise HTTPException(
#             status_code=status.HTTP_401_UNAUTHORIZED,
#             detail="Could not validate credentials",
#         )


def verify_token(access_token: str) -> Optional[Dict[str, Any]]:
    """
    Verify a Supabase access token and return user information.
    
    Args:
        access_token (str): The JWT access token to verify
        
    Returns:
        Optional[Dict[str, Any]]: User data if token is valid, None otherwise
    """
    if not access_token:
        logger.warning("No access token provided for verification")
        return None
    
    try:
        # Get Supabase client
        supabase = get_supabase_client()
        if not supabase:
            logger.error("Failed to initialize Supabase client")
            return None
        
        # Verify token and get user
        user_response = supabase.auth.get_user(access_token)
        
        if not user_response or not user_response.user:
            logger.warning("Token verification returned no user data")
            return None
        
        user_data = {
            "id": user_response.user.id,
            "email": user_response.user.email,
            "email_confirmed_at": user_response.user.email_confirmed_at,
            "created_at": user_response.user.created_at,
            "updated_at": user_response.user.updated_at,
            "role": user_response.user.role,
            "aud": user_response.user.aud,
            "user_metadata": user_response.user.user_metadata,
            "app_metadata": user_response.user.app_metadata
        }
        
        logger.info(f"Token verified successfully for user: {user_data['email']}")
        return user_data
        
    except Exception as e:
        # Handle all authentication errors gracefully
        error_msg = str(e)
        if "Session from session_id claim in JWT does not exist" in error_msg:
            logger.warning("Token verification failed - session expired or invalid")
        elif "403 Forbidden" in error_msg:
            logger.warning("Token verification failed - access forbidden (likely expired)")
        elif "401 Unauthorized" in error_msg:
            logger.warning("Token verification failed - unauthorized")
        else:
            logger.error(f"Token verification failed with unexpected error: {error_msg}")
        
        # Always return None for any authentication failure
        # This prevents exceptions from propagating and causing connection issues
        return None

def is_token_valid(access_token: str) -> bool:
    """
    Check if a Supabase access token is valid (boolean version).
    
    Args:
        access_token (str): The JWT access token to verify
        
    Returns:
        bool: True if token is valid, False otherwise
    """
    return verify_token(access_token) is not None
