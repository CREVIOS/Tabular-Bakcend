from fastapi import APIRouter, Depends, HTTPException, status
from typing import List, Optional
import uuid
from datetime import datetime
from core.supabase_create import get_supabase_admin
from api.auth import get_current_user
from pydantic import BaseModel
import re

router = APIRouter()

# Pydantic models
class FolderCreate(BaseModel):
    name: str
    description: Optional[str] = None
    color: Optional[str] = '#3b82f6'

class FolderUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    color: Optional[str] = None

class FolderResponse(BaseModel):
    id: str
    user_id: str
    name: str
    description: Optional[str]
    color: str
    file_count: int
    total_size: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

def parse_datetime_safely(datetime_str: str) -> datetime:
    """Safely parse datetime string with flexible microsecond handling"""
    try:
        # First try the standard fromisoformat with Z replacement
        if datetime_str.endswith('Z'):
            datetime_str = datetime_str.replace('Z', '+00:00')
        
        # Handle microseconds - ensure they are exactly 6 digits
        # Use regex to find and fix microseconds part
        microsecond_pattern = r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.(\d+)([\+\-]\d{2}:\d{2}|Z?)$'
        match = re.match(microsecond_pattern, datetime_str)
        
        if match:
            date_time_part = match.group(1)
            microseconds = match.group(2)
            timezone_part = match.group(3)
            
            # Pad microseconds to exactly 6 digits or truncate if longer
            if len(microseconds) < 6:
                microseconds = microseconds.ljust(6, '0')
            elif len(microseconds) > 6:
                microseconds = microseconds[:6]
            
            # Reconstruct the datetime string
            datetime_str = f"{date_time_part}.{microseconds}{timezone_part}"
        
        return datetime.fromisoformat(datetime_str)
    except ValueError:
        # Fallback: try parsing without microseconds
        try:
            # Remove microseconds completely
            datetime_str = re.sub(r'\.\d+', '', datetime_str)
            return datetime.fromisoformat(datetime_str)
        except ValueError:
            # Last resort: try strptime with common formats
            for fmt in [
                "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S.%f%z",
                "%Y-%m-%dT%H:%M:%SZ",
                "%Y-%m-%dT%H:%M:%S.%fZ"
            ]:
                try:
                    if datetime_str.endswith('Z'):
                        datetime_str = datetime_str[:-1] + '+00:00'
                    return datetime.strptime(datetime_str, fmt)
                except ValueError:
                    continue
            # If all fails, raise the original error
            raise ValueError(f"Unable to parse datetime: {datetime_str}")

@router.get("/", response_model=List[FolderResponse])
async def get_folders(current_user = Depends(get_current_user)):
    """Get all folders for the current user with file counts and sizes"""
    try:
        supabase_admin = get_supabase_admin()
        
        # Get folders with file statistics (fetch file_size for each file; count computed in Python)
        folders_response = supabase_admin.table("folders")\
            .select("*, files(file_size)")\
            .eq("user_id", current_user.id)\
            .order("name")\
            .execute()
        
        
        # Gracefully handle case where the folders table (or relationship) does not exist yet.
        if hasattr(folders_response, 'error') and folders_response.error:
            error_msg = str(folders_response.error)
            # If the project does not have a folders table yet, just return an empty list
            # instead of propagating a 500 so that the dashboard can still load.
            if "relation \"folders\"" in error_msg or "does not exist" in error_msg:
                print("[folders] Table missing, returning empty list to client")
                return []

            # For other errors keep the original behaviour
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to fetch folders: {folders_response.error}"
            )
        
        folders = []
        for folder_data in folders_response.data:
            # Calculate file count and total size
            file_count = len(folder_data.get('files', []))
            total_size = sum(f.get('file_size', 0) or 0 for f in folder_data.get('files', []))
            
            # Parse timestamps
            created_at = parse_datetime_safely(folder_data['created_at'].replace('Z', '+00:00'))
            updated_at = parse_datetime_safely(folder_data['updated_at'].replace('Z', '+00:00'))
            
            folders.append(FolderResponse(
                id=folder_data['id'],
                user_id=folder_data['user_id'],
                name=folder_data['name'],
                description=folder_data.get('description'),
                color=folder_data.get('color', '#3b82f6'),
                file_count=file_count,
                total_size=total_size,
                created_at=created_at,
                updated_at=updated_at
            ))
        
        return folders
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch folders: {str(e)}"
        )

@router.post("/", response_model=FolderResponse)
async def create_folder(folder: FolderCreate, current_user = Depends(get_current_user)):
    """Create a new folder"""
    try:
        supabase_admin = get_supabase_admin()
        
        # Check if folder name already exists for this user
        existing_response = supabase_admin.table("folders")\
            .select("id")\
            .eq("user_id", current_user.id)\
            .eq("name", folder.name)\
            .execute()
        
        if existing_response.data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A folder with this name already exists"
            )
        
        # Create folder
        folder_data = {
            "user_id": current_user.id,
            "name": folder.name,
            "description": folder.description,
            "color": folder.color,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat()
        }
        
        response = supabase_admin.table("folders").insert(folder_data).execute()
        
        if hasattr(response, 'error') and response.error:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to create folder: {response.error}"
            )
        
        created_folder = response.data[0]
        
        return FolderResponse(
            id=created_folder['id'],
            user_id=created_folder['user_id'],
            name=created_folder['name'],
            description=created_folder.get('description'),
            color=created_folder.get('color', '#3b82f6'),
            file_count=0,
            total_size=0,
            created_at=parse_datetime_safely(created_folder['created_at'].replace('Z', '+00:00')),
            updated_at=parse_datetime_safely(created_folder['updated_at'].replace('Z', '+00:00'))
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create folder: {str(e)}"
        )

@router.put("/{folder_id}", response_model=FolderResponse)
async def update_folder(folder_id: str, folder_update: FolderUpdate, current_user = Depends(get_current_user)):
    """Update a folder"""
    try:
        supabase_admin = get_supabase_admin()
        
        # Verify folder exists and belongs to user
        folder_response = supabase_admin.table("folders")\
            .select("*")\
            .eq("id", folder_id)\
            .eq("user_id", current_user.id)\
            .execute()
        
        if not folder_response.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Folder not found"
            )
        
        # Check for name conflicts if name is being updated
        if folder_update.name:
            existing_response = supabase_admin.table("folders")\
                .select("id")\
                .eq("user_id", current_user.id)\
                .eq("name", folder_update.name)\
                .neq("id", folder_id)\
                .execute()
            
            if existing_response.data:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="A folder with this name already exists"
                )
        
        # Prepare update data
        update_data = {"updated_at": datetime.utcnow().isoformat()}
        if folder_update.name is not None:
            update_data["name"] = folder_update.name
        if folder_update.description is not None:
            update_data["description"] = folder_update.description
        if folder_update.color is not None:
            update_data["color"] = folder_update.color
        
        # Update folder
        response = supabase_admin.table("folders")\
            .update(update_data)\
            .eq("id", folder_id)\
            .execute()
        
        if hasattr(response, 'error') and response.error:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update folder: {response.error}"
            )
        
        # Get updated folder with file count
        updated_folder = response.data[0]
        
        # Get file count and total size
        files_response = supabase_admin.table("files")\
            .select("file_size")\
            .eq("folder_id", folder_id)\
            .execute()
        
        file_count = len(files_response.data)
        total_size = sum(f.get('file_size', 0) or 0 for f in files_response.data)
        
        return FolderResponse(
            id=updated_folder['id'],
            user_id=updated_folder['user_id'],
            name=updated_folder['name'],
            description=updated_folder.get('description'),
            color=updated_folder.get('color', '#3b82f6'),
            file_count=file_count,
            total_size=total_size,
            created_at=parse_datetime_safely(updated_folder['created_at'].replace('Z', '+00:00')),
            updated_at=parse_datetime_safely(updated_folder['updated_at'].replace('Z', '+00:00'))
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to update folder: {str(e)}"
        )

@router.delete("/{folder_id}")
async def delete_folder(folder_id: str, current_user = Depends(get_current_user)):
    """Delete a folder (files will be moved to uncategorized)"""
    try:
        supabase_admin = get_supabase_admin()
        
        # Verify folder exists and belongs to user
        folder_response = supabase_admin.table("folders")\
            .select("*")\
            .eq("id", folder_id)\
            .eq("user_id", current_user.id)\
            .execute()
        
        if not folder_response.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Folder not found"
            )
        
        # Check if there are files in this folder
        files_response = supabase_admin.table("files")\
            .select("id")\
            .eq("folder_id", folder_id)\
            .execute()
        
        if files_response.data:
            # Move files to null folder_id (uncategorized)
            supabase_admin.table("files")\
                .update({"folder_id": None})\
                .eq("folder_id", folder_id)\
                .execute()
        
        # Delete the folder
        delete_response = supabase_admin.table("folders")\
            .delete()\
            .eq("id", folder_id)\
            .execute()
        
        if hasattr(delete_response, 'error') and delete_response.error:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to delete folder: {delete_response.error}"
            )
        
        return {"message": "Folder deleted successfully", "files_moved": len(files_response.data)}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete folder: {str(e)}"
        )

@router.get("/{folder_id}/files")
async def get_folder_files(folder_id: str, current_user = Depends(get_current_user)):
    """Get all files in a specific folder"""
    try:
        supabase_admin = get_supabase_admin()
        
        # Verify folder exists and belongs to user
        folder_response = supabase_admin.table("folders")\
            .select("id")\
            .eq("id", folder_id)\
            .eq("user_id", current_user.id)\
            .execute()
        
        if not folder_response.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Folder not found"
            )
        
        # Get files in folder
        files_response = supabase_admin.table("files")\
            .select("*")\
            .eq("folder_id", folder_id)\
            .eq("user_id", current_user.id)\
            .order("created_at", desc=True)\
            .execute()
        
        return files_response.data
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch folder files: {str(e)}"
        )