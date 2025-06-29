from fastapi import APIRouter, HTTPException, status, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from datetime import timedelta
from core.supabase_create import get_supabase_client, get_supabase_admin
from core.auth import create_access_token, verify_token
from schemas.auth import UserCreate, UserLogin, Token, User
from core.config import settings

router = APIRouter()
security = HTTPBearer()

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Get current user from Supabase JWT token and custom users table"""
    try:
        print(f"Token received: {credentials.credentials[:20]}...")
        
        # Verify Supabase JWT token
        user_data = verify_token(credentials.credentials)
        if not user_data:
            print("Token verification failed")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
                headers={"WWW-Authenticate": "Bearer"}
            )
        
        user_id = user_data["id"]
        print(f"Token verified for user_id: {user_id}")
        
        # Get user from custom users table (NOT auth.users)
        supabase_admin = get_supabase_admin()
        user_response = supabase_admin.table("users").select("*").eq("id", user_id).execute()
        
        if not user_response.data:
            print(f"User not found in users table: {user_id}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User profile not found",
                headers={"WWW-Authenticate": "Bearer"}
            )
        
        user_data = user_response.data[0]
        print(f"User found: {user_data['email']}")
        
        # Return a simple user object
        class AuthenticatedUser:
            def __init__(self, data):
                self.id = data["id"]
                self.email = data["email"]
                self.full_name = data.get("full_name")
        
        return AuthenticatedUser(user_data)
        
    except HTTPException:
        # Re-raise HTTP exceptions as-is (they contain proper status codes)
        raise
    except Exception as e:
        print(f"Authentication error: {str(e)}")
        # Return a proper 401 response for any other authentication error
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed",
            headers={"WWW-Authenticate": "Bearer"}
        )

@router.post("/register", response_model=Token)
async def register(user_data: UserCreate):
    try:
        supabase_client = get_supabase_client()
        
        # Create user with Supabase Auth
        auth_response = supabase_client.auth.sign_up({
            "email": user_data.email,
            "password": user_data.password,
            "options": {
                "data": {
                    "full_name": user_data.full_name or ""
                }
            }
        })
        
        if auth_response.user is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Registration failed"
            )
        
        print(f"User registered in Supabase Auth: {auth_response.user.id}")
        
        # Create user profile in public.users table
        supabase_admin = get_supabase_admin()
        profile_data = {
            "id": auth_response.user.id,
            "email": auth_response.user.email,
            "full_name": user_data.full_name or "",
        }
        
        # Insert user profile
        profile_response = supabase_admin.table("users").insert(profile_data).execute()
        print(f"User profile created: {profile_response.data}")
        
        # Create custom JWT token (NOT Supabase token)
        access_token_expires = timedelta(minutes=settings.access_token_expire_minutes)
        access_token = create_access_token(
            data={"sub": auth_response.user.id}, 
            expires_delta=access_token_expires
        )
        
        user = User(
            id=auth_response.user.id,
            email=auth_response.user.email,
            full_name=user_data.full_name,
            created_at=auth_response.user.created_at
        )
        
        return Token(
            access_token=access_token,
            token_type="bearer",
            user=user,
            expires_in=settings.access_token_expire_minutes * 60  # Convert to seconds
        )
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Registration error: {str(e)}")
        if "already registered" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered"
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Registration failed"
        )

@router.post("/login", response_model=Token)
async def login(user_data: UserLogin):
    try:
        supabase_client = get_supabase_client()
        
        # Authenticate with Supabase
        auth_response = supabase_client.auth.sign_in_with_password({
            "email": user_data.email,
            "password": user_data.password
        })
        
        if auth_response.user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials"
            )
        
        print(f"User authenticated: {auth_response.user.id}")
        
        # Get or create user profile
        supabase_admin = get_supabase_admin()
        profile_response = supabase_admin.table("users").select("*").eq("id", auth_response.user.id).execute()
        
        if not profile_response.data:
            # Create missing profile
            profile_data = {
                "id": auth_response.user.id,
                "email": auth_response.user.email,
                "full_name": auth_response.user.user_metadata.get("full_name", "") if auth_response.user.user_metadata else "",
            }
            profile_response = supabase_admin.table("users").insert(profile_data).execute()
            profile = profile_data
            print(f"Created missing user profile: {auth_response.user.id}")
        else:
            profile = profile_response.data[0]
            print(f"Found existing user profile: {profile['id']}")
        
        # Create custom JWT token
        access_token_expires = timedelta(minutes=settings.access_token_expire_minutes)
        access_token = create_access_token(
            data={"sub": auth_response.user.id}, 
            expires_delta=access_token_expires
        )
        
        user = User(
            id=auth_response.user.id,
            email=profile["email"],
            full_name=profile.get("full_name"),
            created_at=profile["created_at"]
        )
        
        return Token(
            access_token=access_token,
            token_type="bearer",
            user=user,
            expires_in=settings.access_token_expire_minutes * 60  # Convert to seconds
        )
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Login error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials"
        )

@router.get("/verify")
async def verify_token_endpoint(current_user = Depends(get_current_user)):
    """Verify if the current token is valid and return user info"""
    try:
        return {
            "valid": True,
            "user": {
                "id": current_user.id,
                "email": current_user.email,
                "full_name": getattr(current_user, 'full_name', None)
            }
        }
    except Exception as e:
        print(f"Token verification error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )

@router.post("/logout")
async def logout(current_user = Depends(get_current_user)):
    """Logout endpoint - in a stateless JWT system, this just validates the token"""
    try:
        # In a stateless JWT system, we can't really invalidate tokens
        # But we can log the logout event and return success
        print(f"User {current_user.email} logged out")
        
        return {
            "message": "Successfully logged out",
            "user_id": current_user.id
        }
    except Exception as e:
        print(f"Logout error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )

@router.post("/refresh")
async def refresh_token(current_user = Depends(get_current_user)):
    """Refresh the access token"""
    try:
        # Create a new access token
        access_token_expires = timedelta(minutes=settings.access_token_expire_minutes)
        access_token = create_access_token(
            data={"sub": current_user.id}, 
            expires_delta=access_token_expires
        )
        
        # Get fresh user data
        supabase_admin = get_supabase_admin()
        user_response = supabase_admin.table("users").select("*").eq("id", current_user.id).execute()
        
        if not user_response.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        
        user_data = user_response.data[0]
        user = User(
            id=user_data["id"],
            email=user_data["email"],
            full_name=user_data.get("full_name"),
            created_at=user_data["created_at"]
        )
        
        return Token(
            access_token=access_token,
            token_type="bearer",
            user=user,
            expires_in=settings.access_token_expire_minutes * 60  # Convert to seconds
        )
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Token refresh error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not refresh token"
        )