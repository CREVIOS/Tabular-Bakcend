from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status, Request, Form
from fastapi.security import HTTPBearer
from typing import List, Optional
import uuid
from datetime import datetime
from core.supabase_create import get_supabase_admin
from schemas.files import FileResponse, MarkdownResponse
from api.auth import get_current_user
from tasks.document_processor import process_document_task
from core.auth import verify_token

router = APIRouter()

@router.post("/upload", response_model=List[FileResponse])
async def upload_files(request: Request):
    """
    Upload files with optional folder assignment
    """
    print("Upload endpoint called - starting manual processing")
    
    # Manual authentication (keeping existing auth logic)
    authorization = request.headers.get("authorization")
    if not authorization or not authorization.startswith("Bearer "):
        print("No authorization header found")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header required"
        )
    
    token = authorization[7:]  # Remove "Bearer " prefix
    
    try:
        print(f"Manual auth - Token received: {token[:20]}...")
        
        # Verify Supabase JWT token
        user_data = verify_token(token)
        if not user_data:
            print("Manual auth - Token verification failed")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token"
            )
        
        user_id = user_data["id"]
        print(f"Manual auth - Token verified for user_id: {user_id}")
        
        # Get user from custom users table
        supabase_admin = get_supabase_admin()
        user_response = supabase_admin.table("users").select("*").eq("id", user_id).execute()
        
        if not user_response.data:
            print(f"Manual auth - User not found in users table: {user_id}")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="User profile not found"
            )
        
        user_data = user_response.data[0]
        print(f"Manual auth - User found: {user_data['email']}")
        
        # Create user object
        class AuthenticatedUser:
            def __init__(self, data):
                self.id = data["id"]
                self.email = data["email"]
                self.full_name = data.get("full_name")
        
        current_user = AuthenticatedUser(user_data)
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Manual auth - Authentication error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials"
        )
    
    print(f"Upload endpoint - User authenticated: {current_user.email}")
    
    # Manual form parsing with folder support
    try:
        form = await request.form()
        print(f"Form data received: {list(form.keys())}")
        
        # Get folder_id from form data (optional)
        folder_id = form.get('folder_id')
        if folder_id:
            print(f"Folder ID specified: {folder_id}")
            
            # Verify folder belongs to user
            folder_response = supabase_admin.table("folders")\
                .select("id")\
                .eq("id", folder_id)\
                .eq("user_id", current_user.id)\
                .execute()
            
            if not folder_response.data:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid folder ID or folder does not belong to you"
                )
        
        # Get files from form data
        files = []
        for key, value in form.items():
            if key == 'files' and hasattr(value, 'filename'):
                files.append(value)
                print(f"Found file: {value.filename} ({value.content_type}, {value.size} bytes)")
        
        if not files:
            print("No files found in form data")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No files were uploaded. Please select at least one file."
            )
        
        print(f"Processing {len(files)} files for folder: {folder_id or 'No folder'}")
        
    except Exception as e:
        print(f"Form parsing error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to parse form data: {str(e)}"
        )
    
    uploaded_files = []
    
    # File validation (same as before)
    allowed_types = {
        'application/pdf',
        'application/msword', 
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'text/plain',
        'application/vnd.ms-excel',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    }
    
    allowed_extensions = {'.pdf', '.doc', '.docx', '.txt', '.xls', '.xlsx'}
    dangerous_extensions = {'.exe', '.bat', '.sh', '.php', '.js', '.py', '.cmd', '.scr', '.com', '.pif'}
    max_file_size = 50 * 1024 * 1024  # 50MB
    
    for file in files:
        try:
            # Get file extension
            file_extension = '.' + file.filename.split('.')[-1].lower() if '.' in file.filename else ''
            
            # Security validations (same as before)
            if file.size and file.size > max_file_size:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"File {file.filename} is too large. Maximum size is 50MB."
                )
            
            if file_extension in dangerous_extensions:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"File {file.filename} has a potentially dangerous extension."
                )
            
            if file_extension not in allowed_extensions:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"File {file.filename} has unsupported extension. Please upload PDF, Word, Excel, or text files."
                )
            
            # Generate unique filename and storage path
            file_id = str(uuid.uuid4())
            storage_path = f"{current_user.id}/{file_id}_{file.filename}"
            
            # Upload to Supabase Storage
            file_content = await file.read()
            
            # Determine content type based on extension
            content_type = file.content_type
            if file_extension == '.pdf':
                content_type = 'application/pdf'
            elif file_extension in ['.doc', '.docx']:
                content_type = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
            elif file_extension in ['.xls', '.xlsx']:
                content_type = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            elif file_extension == '.txt':
                content_type = 'text/plain'
            
            storage_response = supabase_admin.storage.from_("documents").upload(
                storage_path, 
                file_content, 
                {"content-type": content_type}
            )
            
            if hasattr(storage_response, 'error') and storage_response.error:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to upload {file.filename} to storage: {storage_response.error}"
                )
            
            # Get public URL
            storage_url_response = supabase_admin.storage.from_("documents").get_public_url(storage_path)
            storage_url = storage_url_response if isinstance(storage_url_response, str) else storage_url_response.get('publicUrl', '')
            
            # Create file record in database with folder_id
            file_data = {
                "id": file_id,
                "user_id": current_user.id,
                "folder_id": folder_id,  # Add folder_id to file record
                "original_filename": file.filename,
                "file_size": len(file_content),
                "file_type": content_type,
                "storage_path": storage_path,
                "storage_url": storage_url,
                "status": "queued",
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat(),
                "processed_at": None,
                "error_message": None
            }
            
            db_response = supabase_admin.table("files").insert(file_data).execute()
            
            if hasattr(db_response, 'error') and db_response.error:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to create file record: {db_response.error}"
                )
            
            # Queue document processing task
            process_document_task.delay(file_id)
            
            uploaded_files.append(FileResponse(
                id=file_id,
                user_id=current_user.id,
                original_filename=file.filename,
                file_size=len(file_content),
                file_type=content_type,
                storage_path=storage_path,
                storage_url=storage_url,
                status="queued",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
                processed_at=None,
                error_message=None
            ))
            
            print(f"Successfully processed file: {file.filename} -> folder: {folder_id or 'No folder'}")
            
        except HTTPException:
            raise
        except Exception as e:
            print(f"File processing error for {file.filename}: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to process {file.filename}: {str(e)}"
            )
    
    print(f"Upload completed successfully - {len(uploaded_files)} files processed")
    return uploaded_files

@router.get("/", response_model=List[FileResponse])
async def get_user_files(
    folder_id: Optional[str] = None, 
    page: int = 1, 
    limit: int = 50,
    current_user = Depends(get_current_user)
):
    """Get files for current user, optionally filtered by folder with pagination"""
    try:
        print(f"get_user_files called - user: {current_user.email}, folder_id: {folder_id}, page: {page}, limit: {limit}")
        
        # Validate pagination parameters
        if page < 1:
            page = 1
        if limit < 1 or limit > 100:  # Max 100 files per page
            limit = 50
            
        supabase_admin = get_supabase_admin()
        
        # Build query
        query = supabase_admin.table("files").select("*").eq("user_id", current_user.id)
        
        if folder_id is not None:
            if folder_id == "null" or folder_id == "":
                # Get files not in any folder
                query = query.is_("folder_id", None)
                print("Filtering for files without folder")
            else:
                # Get files in specific folder
                query = query.eq("folder_id", folder_id)
                print(f"Filtering for files in folder: {folder_id}")
        
        # Add pagination
        offset = (page - 1) * limit
        query = query.order("created_at", desc=True).range(offset, offset + limit - 1)
        
        print(f"Executing database query with pagination (offset: {offset}, limit: {limit})...")
        response = query.execute()
        
        if hasattr(response, 'error') and response.error:
            print(f"Database query failed: {response.error}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to fetch files: {response.error}"
            )
        
        files_count = len(response.data)
        print(f"Database query successful - found {files_count} files for this page")
        
        files = []
        for i, file_data in enumerate(response.data):
            # Parse dates safely (simplified for performance)
            created_at = None
            processed_at = None
            updated_at = None
            
            try:
                if file_data.get("created_at"):
                    created_at_str = file_data["created_at"]
                    if created_at_str.endswith('Z'):
                        created_at_str = created_at_str.replace('Z', '+00:00')
                    created_at = datetime.fromisoformat(created_at_str)
                
                if file_data.get("processed_at"):
                    processed_at_str = file_data["processed_at"]
                    if processed_at_str.endswith('Z'):
                        processed_at_str = processed_at_str.replace('Z', '+00:00')
                    processed_at = datetime.fromisoformat(processed_at_str)
                
                if file_data.get("updated_at"):
                    updated_at_str = file_data["updated_at"]
                    if updated_at_str.endswith('Z'):
                        updated_at_str = updated_at_str.replace('Z', '+00:00')
                    updated_at = datetime.fromisoformat(updated_at_str)
                        
            except (ValueError, TypeError) as e:
                created_at = datetime.utcnow()
                updated_at = datetime.utcnow()
            
            # Create optimized response with only essential data
            files.append(FileResponse(
                id=file_data["id"],
                user_id=file_data["user_id"],
                original_filename=file_data["original_filename"],
                file_size=file_data.get("file_size"),
                file_type=file_data.get("file_type"),
                storage_path=file_data.get("storage_path"),
                storage_url=file_data.get("storage_url"),
                status=file_data["status"],
                created_at=created_at or datetime.utcnow(),
                updated_at=updated_at or datetime.utcnow(),
                processed_at=processed_at,
                error_message=file_data.get("error_message")
            ))
        
        print(f"Successfully processed {len(files)} files - returning response (page {page})")
        return files
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Unexpected error in get_user_files: {str(e)}")
        import traceback
        print(f"Full traceback: {traceback.format_exc()}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch files: {str(e)}"
        )

@router.put("/{file_id}/move")
async def move_file_to_folder(file_id: str, folder_id: Optional[str] = None, current_user = Depends(get_current_user)):
    """Move a file to a different folder"""
    try:
        supabase_admin = get_supabase_admin()
        
        # Verify file exists and belongs to user
        file_response = supabase_admin.table("files")\
            .select("id")\
            .eq("id", file_id)\
            .eq("user_id", current_user.id)\
            .execute()
        
        if not file_response.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File not found"
            )
        
        # If folder_id is provided, verify it belongs to user
        if folder_id:
            folder_response = supabase_admin.table("folders")\
                .select("id")\
                .eq("id", folder_id)\
                .eq("user_id", current_user.id)\
                .execute()
            
            if not folder_response.data:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid folder ID or folder does not belong to you"
                )
        
        # Update file's folder_id
        update_response = supabase_admin.table("files")\
            .update({"folder_id": folder_id, "updated_at": datetime.utcnow().isoformat()})\
            .eq("id", file_id)\
            .execute()
        
        if hasattr(update_response, 'error') and update_response.error:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to move file: {update_response.error}"
            )
        
        return {"message": "File moved successfully", "folder_id": folder_id}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to move file: {str(e)}"
        )

# Keep existing markdown endpoint unchanged
@router.get("/{file_id}/markdown", response_model=MarkdownResponse)
async def get_file_markdown(file_id: str, current_user = Depends(get_current_user)):
    """Get markdown content for a file"""
    try:
        # First verify the file belongs to the user
        supabase_admin = get_supabase_admin()
        file_response = supabase_admin.table("files").select("id").eq("id", file_id).eq("user_id", current_user.id).execute()
        
        if hasattr(file_response, 'error') and file_response.error:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to verify file ownership"
            )
        
        if not file_response.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File not found"
            )
        
        # Get markdown content for file
        response = supabase_admin.table("markdown_content").select("*").eq("file_id", file_id).eq("user_id", current_user.id).execute()
        
        if hasattr(response, 'error') and response.error:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to fetch markdown content"
            )
        
        if not response.data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Markdown content not found"
            )
        
        markdown_data = response.data[0]
        
        # Parse created_at safely
        created_at = None
        try:
            if markdown_data.get("created_at"):
                created_at_str = markdown_data["created_at"]
                if created_at_str.endswith('Z'):
                    created_at_str = created_at_str.replace('Z', '+00:00')
                created_at = datetime.fromisoformat(created_at_str)
        except (ValueError, TypeError):
            created_at = datetime.utcnow()
        
        return MarkdownResponse(
            id=markdown_data["id"],
            file_id=markdown_data["file_id"],
            user_id=markdown_data["user_id"],
            content=markdown_data["content"],
            word_count=markdown_data.get("word_count"),
            created_at=created_at or datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch markdown content: {str(e)}"
        )