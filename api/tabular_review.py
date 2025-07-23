from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, HTTPException, BackgroundTasks, Query, Response
from fastapi.responses import StreamingResponse
from typing import Dict, Set, List, Optional
import asyncio
import json
import uuid
from datetime import datetime, timedelta
import redis.asyncio as redis
from concurrent.futures import ThreadPoolExecutor
import google.generativeai as genai
from dataclasses import dataclass, asdict
import msgpack
import time
import os
import csv
import io
from dateutil.parser import isoparse
import threading
import queue
from collections import defaultdict, deque

from core.supabase_create import get_supabase_admin
from schemas.tabular_reviews import (
    TabularReviewCreate,
    TabularReviewResponse,
    TabularReviewDetailResponse,
    TabularReviewListResponse,
    TabularReviewUpdate,
    TabularReviewColumnResponse,
    TabularReviewColumnUpdate,
    TabularReviewFileResponse,
    TabularReviewResultResponse,
    TabularReviewResultUpdate,
    AnalysisRequest,
    AnalysisStatus,
    AddFilesToReviewRequest,
    AddColumnToReviewRequest,
    ExportRequest,
    ExportFormat,
    BulkValidationRequest,
    ValidationStats,
    ReviewSummary,
    ReviewScope,
    ReviewStatus
)
from api.auth import get_current_user, verify_token

router = APIRouter()

# Initialize Gemini
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

def parse_iso_datetime(dt_str: str) -> datetime:
    """Robustly parse an ISO‐8601 string into a datetime"""
    return isoparse(dt_str.strip())

# ============================================================================
# REDIS AND CONNECTION POOLS
# ============================================================================

# Connection pools
redis_pool = redis.ConnectionPool(
    host=os.getenv('REDIS_HOST', 'redis'), 
    port=int(os.getenv('REDIS_PORT', 6379)), 
    decode_responses=False,  # Use msgpack for better performance
    max_connections=100
)
redis_client = redis.Redis(connection_pool=redis_pool)

# Thread pool for CPU-intensive tasks
executor = ThreadPoolExecutor(max_workers=20)

# ============================================================================
# WEBSOCKET CONNECTION MANAGER
# ============================================================================

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, Set[WebSocket]] = {}
        self.user_connections: Dict[str, WebSocket] = {}
        
    async def connect(self, websocket: WebSocket, user_id: str, review_id: str):
        await websocket.accept()
        
        # Store connection
        key = f"{user_id}:{review_id}"
        self.user_connections[key] = websocket
        
        if user_id not in self.active_connections:
            self.active_connections[user_id] = set()
        self.active_connections[user_id].add(websocket)
        
        print(f"🔌 WebSocket connected: {user_id} -> {review_id}")
        
    def disconnect(self, websocket: WebSocket, user_id: str):
        if user_id in self.active_connections:
            self.active_connections[user_id].discard(websocket)
            
        # Clean up user connections
        keys_to_remove = []
        for key, ws in self.user_connections.items():
            if ws == websocket:
                keys_to_remove.append(key)
        for key in keys_to_remove:
            del self.user_connections[key]
            
        print(f"🔌 WebSocket disconnected: {user_id}")
    
    async def send_to_user(self, user_id: str, data: dict):
        """Send data to all connections for a user"""
        if user_id in self.active_connections:
            # Use msgpack for faster serialization
            message = msgpack.packb(data)
            
            # Send to all user connections concurrently
            tasks = []
            dead_connections = set()
            
            for connection in self.active_connections[user_id]:
                try:
                    tasks.append(connection.send_bytes(message))
                except:
                    dead_connections.add(connection)
            
            # Remove dead connections
            for dead_conn in dead_connections:
                self.active_connections[user_id].discard(dead_conn)
            
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
    
    async def send_to_review(self, user_id: str, review_id: str, data: dict):
        """Send data to specific review connection"""
        key = f"{user_id}:{review_id}"
        if key in self.user_connections:
            message = msgpack.packb(data)
            try:
                await self.user_connections[key].send_bytes(message)
            except:
                # Connection might be closed, clean up
                del self.user_connections[key]

manager = ConnectionManager()

# ============================================================================
# OPTIMIZED DATA STRUCTURES
# ============================================================================

@dataclass
class CellUpdate:
    review_id: str
    file_id: str
    column_id: str
    value: Optional[str]
    confidence: float
    timestamp: float

# ============================================================================
# HIGH-PERFORMANCE CELL PROCESSOR
# ============================================================================

class CellProcessor:
    def __init__(self):
        self.processing_queue = asyncio.Queue(maxsize=1000)
        self.result_queue = asyncio.Queue(maxsize=1000)
        self.batch_size = 10
        
    async def process_batch(self, batch: List[dict]):
        """Process multiple cells in parallel"""
        tasks = []
        for item in batch:
            task = asyncio.create_task(self._process_single_cell(item))
            tasks.append(task)
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in results if not isinstance(r, Exception)]
    
    async def _process_single_cell(self, item: dict):
        """Process a single cell with Gemini"""
        try:
            # Get document content from Redis cache first
            cache_key = f"doc:{item['file_id']}"
            cached_content = await redis_client.get(cache_key)
            
            if cached_content:
                document_content = msgpack.unpackb(cached_content).decode('utf-8')
            else:
                # Fetch from Supabase if not cached
                document_content = await self._fetch_document_content(item['file_id'], item['user_id'])
                # Cache for 1 hour
                if document_content:
                    await redis_client.setex(cache_key, 3600, msgpack.packb(document_content.encode('utf-8')))
            
            if not document_content:
                return {
                    'review_id': item['review_id'],
                    'file_id': item['file_id'],
                    'column_id': item['column_id'],
                    'short_answer': None,
                    'long_answer': None,
                    'confidence': 0.0,
                    'source': 'Document content not found',
                    'timestamp': time.time(),
                    'retry_needed': True
                }
            
            # Call Gemini (in thread pool to not block)
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                executor,
                self._gemini_extract,
                document_content,
                item['prompt'],
                item.get('column_name', ''),
                item.get('data_type', 'text')
            )
            
            # Check if we got a valid response
            retry_needed = False
            if (result.get('short_answer') is None and result.get('long_answer') is None) or result.get('confidence', 0) == 0.0:
                retry_needed = True
            
            return {
                'review_id': item['review_id'],
                'file_id': item['file_id'],
                'column_id': item['column_id'],
                'short_answer': result.get('short_answer'),
                'long_answer': result.get('long_answer'),
                'confidence': result.get('confidence', 0.0),
                'source': result.get('source', ''),
                'timestamp': time.time(),
                'retry_needed': retry_needed,
                'prompt': item.get('prompt', ''),
                'column_name': item.get('column_name', ''),
                'data_type': item.get('data_type', 'text'),
                'user_id': item.get('user_id', '')
            }
        
            
        except Exception as e:
            print(f"Cell processing error: {e}")
            return {
                'review_id': item['review_id'],
                'file_id': item['file_id'],
                'column_id': item['column_id'],
                'short_answer': None,
                'long_answer': None,
                'confidence': 0.0,
                'source': f'Processing error: {str(e)}',
                'timestamp': time.time(),
                'retry_needed': True,
                'prompt': item.get('prompt', ''),
                'column_name': item.get('column_name', ''),
                'data_type': item.get('data_type', 'text'),
                'user_id': item.get('user_id', '')
            }
    
    def _gemini_extract(self, content: str, prompt: str, column_name: str, data_type: str) -> dict:
        """Synchronous Gemini call for thread pool"""
        try:
            model = genai.GenerativeModel('gemini-2.5-pro')
            
            analysis_prompt = f"""
    You are an expert document analyst with exceptional attention to detail. Your task is to extract specific information from the provided document based on precise analysis requirements.

    DOCUMENT CONTENT:
    {content}

    ANALYSIS REQUIREMENT:
    • Extraction Requirement: {prompt}
    • Expected Data Type: {data_type}

    CRITICAL INSTRUCTIONS:
    1. ONLY extract information that is EXPLICITLY stated in the document
    2. Do NOT infer, assume, or extrapolate beyond what is clearly written
    3. Do NOT hallucinate or create information not present in the document
    4. If information is ambiguous or unclear, treat as not found
    5. Check the document CAREFULLY and THOROUGHLY before responding

    RESPONSE REQUIREMENTS:
    You must provide TWO versions of your answer:
    - SHORT_ANSWER: A concise, if possible just give the answer, no extra words. (1-2 sentences maximum if needed).
    - LONG_ANSWER: A detailed explanation with full context and supporting details

    VALUE ASSIGNMENT RULES:
    - If relevant information is found: Extract the precise value
    - If NO relevant information is found: Return "No Valid Data"
    - If information exists but is unclear/incomplete: Return "No Valid Data"

    CONFIDENCE SCORING (0.0 to 1.0):
    - 0.95-1.0: Information is explicitly stated, unambiguous, and directly matches requirement
    - 0.85-0.94: Information is clearly stated with minor interpretation needed
    - 0.70-0.84: Information is present but requires some interpretation
    - 0.50-0.69: Information is implied or requires significant interpretation
    - 0.30-0.49: Information is vague or partially relevant
    - 0.0-0.29: Information is unclear, contradictory, or barely relevant

    DATA TYPE FORMATTING:
    - text: Return as plain string, no special formatting
    - number: Return as numeric value (integer or decimal)
    - date: Return in YYYY-MM-DD format if possible
    - boolean: Return as true/false
    - currency: Return numeric value without currency symbols
    - percentage: Return as decimal (e.g., 0.25 for 25%)

    SOURCE REFERENCE:
    - Provide specific location: "Page X, Paragraph Y" or "Section Z, Line N"
    - Include relevant surrounding context if helpful
    - For tables: "Table X, Row Y, Column Z"
    - For headers/titles: "Header: [title name]"
    -  The source should be valid chunk  with renference. Like the page, then section, the paragraph, then the chunk for validity. If there is multiple, have them.

    RESPONSE FORMAT:
    Return ONLY a valid JSON object with this exact structure:

    {{
        "short_answer": "concise_value_or_No Valid Data",
        "long_answer": "detailed_explanation_with_context_or_No Valid Data",
        "confidence": 0.95,
        "source": "Page 1, Section 2.1, Paragraph 3, the chunk texts"
    }}

    CRITICAL: Your response must be valid JSON only. No other text, explanations, or markdown formatting.
    """
            
            response = model.generate_content(analysis_prompt)
            response_text = response.text.strip()
            
            # Clean the response to extract just the JSON
            if response_text.startswith('```json'):
                response_text = response_text[7:]
            if response_text.endswith('```'):
                response_text = response_text[:-3]
            
            try:
                extraction = json.loads(response_text)
                
                # Validate response structure
                required_keys = ['short_answer', 'long_answer', 'confidence', 'source']
                if not all(key in extraction for key in required_keys):
                    print(f"Invalid response structure: {extraction}")
                    return {
                        "short_answer": None, 
                        "long_answer": None, 
                        "confidence": 0.0, 
                        "source": "Analysis failed - invalid response structure"
                    }
                
                # Validate confidence score
                confidence = extraction.get('confidence', 0.0)
                if not isinstance(confidence, (int, float)) or confidence < 0 or confidence > 1:
                    extraction['confidence'] = 0.0
                
                return {
                    "short_answer": extraction.get('short_answer'),
                    "long_answer": extraction.get('long_answer'),
                    "confidence": extraction.get('confidence', 0.0),
                    "source": extraction.get('source', "")
                }
                
            except json.JSONDecodeError:
                print(f"Failed to parse Gemini response as JSON: {response_text}")
                return {
                    "short_answer": None, 
                    "long_answer": None, 
                    "confidence": 0.0, 
                    "source": "Analysis failed - JSON parse error"
                }
                
        except Exception as e:
            print(f"Gemini analysis failed: {str(e)}")
            return {
                "short_answer": None, 
                "long_answer": None, 
                "confidence": 0.0, 
                "source": f"Analysis failed: {str(e)}"
            }
    
    async def _fetch_document_content(self, file_id: str, user_id: str) -> str:
        """Fetch document content from Supabase"""
        try:
            supabase = get_supabase_admin()
            markdown_response = supabase.table("markdown_content").select("content").eq("file_id", file_id).eq("user_id", user_id).execute()
            
            if markdown_response.data:
                return markdown_response.data[0]["content"]
            return ""
        except Exception as e:
            print(f"Error fetching document content: {e}")
            return ""

processor = CellProcessor()

# ============================================================================
# BACKGROUND WORKERS
# ============================================================================

async def processing_worker():
    """High-performance background worker"""
    while True:
        batch = []
        
        try:
            # Wait for at least one item
            item = await processor.processing_queue.get()
            batch.append(item)
            
            # Try to fill the batch without waiting
            for _ in range(processor.batch_size - 1):
                try:
                    item = processor.processing_queue.get_nowait()
                    batch.append(item)
                except asyncio.QueueEmpty:
                    break
            
            # Process batch
            if batch:
                results = await processor.process_batch(batch)
                
                # Send results
                for result in results:
                    if result:
                        await processor.result_queue.put(result)
                        
        except Exception as e:
            print(f"Worker error: {e}")
            await asyncio.sleep(0.1)



async def store_result_in_database(result: dict):
    """Store analysis result in Supabase"""
    try:
        supabase = get_supabase_admin()
        
        result_record = {
            "id": str(uuid.uuid4()),
            "review_id": result['review_id'],
            "file_id": result['file_id'],
            "column_id": result['column_id'],
            "extracted_value": result.get('short_answer'),
            "long": result.get('long_answer'),
            "confidence_score": result.get('confidence', 0.0),
            "source_reference": result.get('source', ''),
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat()
        }
        
        # Upsert result
        supabase.table("tabular_review_results").upsert(
            result_record, 
            on_conflict="review_id,file_id,column_id"
        ).execute()
        
        print(f"✅ Stored result: {result['file_id']} x {result['column_id']}")
        
    except Exception as e:
        print(f"Error storing result: {e}")

# Add retry tracking
retry_queue = asyncio.Queue(maxsize=500)

async def result_sender_worker():
    """Send results to WebSocket clients and store in database with retry logic"""
    while True:
        try:
            result = await processor.result_queue.get()
            
            # Check if retry is needed
            if result.get('retry_needed', False):
                # Add to retry queue with a delay
                await asyncio.sleep(2)  # Wait 2 seconds before retry
                await retry_queue.put(result)
                continue
            
            # Store result in Supabase
            await store_result_in_database(result)
            
            # Send update via WebSocket
            update = {
                'type': 'cell_update',
                'data': {
                    'review_id': result['review_id'],
                    'file_id': result['file_id'],
                    'column_id': result['column_id'],
                    'short_answer': result.get('short_answer'),
                    'long_answer': result.get('long_answer'),
                    'confidence': result.get('confidence', 0.0),
                    'source': result.get('source', ''),
                    'timestamp': result['timestamp']
                }
            }
            
            # Get user_id from review
            user_id = await get_review_user(result['review_id'])
            if user_id:
                await manager.send_to_review(user_id, result['review_id'], update)
                
            # Completion check and status update (same as before)
            supabase = get_supabase_admin()
            review_id = result['review_id']
            # Get total columns
            columns_response = supabase.table("tabular_review_columns").select("id", count="exact").eq("review_id", review_id).execute()
            total_columns = columns_response.count or 0
            # Get total files
            files_response = supabase.table("tabular_review_files").select("file_id").eq("review_id", review_id).execute()
            total_files = len(files_response.data)
            # Get total results
            results_response = supabase.table("tabular_review_results").select("id", count="exact").eq("review_id", review_id).execute()
            total_results = results_response.count or 0
            total_expected = total_files * total_columns
            if total_expected > 0 and total_results == total_expected:
                # Mark review as completed
                supabase.table("tabular_reviews").update({
                    "status": "completed",
                    "last_processed_at": datetime.utcnow().isoformat(),
                    "updated_at": datetime.utcnow().isoformat()
                }).eq("id", review_id).execute()
                # Send completion notification
                if user_id:
                    await manager.send_to_review(user_id, review_id, {
                        'type': 'review_completed',
                        'message': 'Review analysis completed!',
                        'timestamp': datetime.utcnow().isoformat()
                    })
        
        except Exception as e:
            print(f"Result sender error: {e}")
            await asyncio.sleep(0.1)

async def retry_worker():
    """Handle retry logic for failed cells"""
    retry_attempts = {}
    max_retries = 3
    
    while True:
        try:
            result = await retry_queue.get()
            
            key = f"{result['review_id']}:{result['file_id']}:{result['column_id']}"
            attempts = retry_attempts.get(key, 0)
            
            if attempts < max_retries:
                retry_attempts[key] = attempts + 1
                print(f"🔄 Retrying cell {key}, attempt {attempts + 1}")
                
                # Re-queue for processing with all necessary data
                await processor.processing_queue.put({
                    'review_id': result['review_id'],
                    'file_id': result['file_id'],
                    'column_id': result['column_id'],
                    'prompt': result.get('prompt', ''),
                    'column_name': result.get('column_name', ''),
                    'data_type': result.get('data_type', 'text'),
                    'user_id': result.get('user_id', '')
                })
            else:
                print(f"❌ Max retries reached for cell {key}")
                # Store failed result to mark as processed
                await store_result_in_database({
                    'review_id': result['review_id'],
                    'file_id': result['file_id'],
                    'column_id': result['column_id'],
                    'short_answer': 'Processing failed',
                    'long_answer': 'Processing failed after multiple attempts',
                    'confidence': 0.0,
                    'source': 'Max retries exceeded',
                    'timestamp': time.time()
                })
                
        except Exception as e:
            print(f"Retry worker error: {e}")
            await asyncio.sleep(1)

# Initialize processor
processor = CellProcessor()

# Function to start background workers - will be called during app startup
async def _redis_listener():
    """Listen for Redis events and process them"""
    # Start background workers
    worker_tasks = []
    for _ in range(5):  
        worker_tasks.append(asyncio.create_task(processing_worker()))
    
    # Add result sender and retry workers
    worker_tasks.append(asyncio.create_task(result_sender_worker()))
    worker_tasks.append(asyncio.create_task(retry_worker()))  # Add retry worker
    
    # Keep the listener running
    while True:
        try:
            await asyncio.sleep(60)  # Just keep the task alive
        except asyncio.CancelledError:
            # Cancel all worker tasks when this task is cancelled
            for task in worker_tasks:
                task.cancel()
            break

async def cleanup_old_buffers():
    """Cleanup old cache entries periodically"""
    while True:
        try:
            await cleanup_old_cache()
            await asyncio.sleep(3600)  # Run every hour
        except asyncio.CancelledError:
            break

# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

async def validate_ws_token(token: str) -> Optional[str]:
    """Validate WebSocket token and return user_id"""
    try:
        user_data = verify_token(token)
        return user_data["id"] if user_data else None
    except:
        return None

async def verify_review_access(review_id: str, user_id: str) -> bool:
    """Check if user has access to review"""
    try:
        # Check in Redis cache first
        cache_key = f"review_access:{user_id}:{review_id}"
        cached_result = await redis_client.get(cache_key)
        
        if cached_result is not None:
            return msgpack.unpackb(cached_result)
        
        # Check in database
        supabase = get_supabase_admin()
        resp = supabase.table("tabular_reviews") \
                      .select("id") \
                      .eq("id", review_id) \
                      .eq("user_id", user_id) \
                      .execute()
        
        has_access = len(resp.data) > 0
        
        # Cache result for 5 minutes
        await redis_client.setex(cache_key, 300, msgpack.packb(has_access))
        
        return has_access
    except Exception as e:
        print(f"Error verifying review access: {e}")
        return False

async def get_review_structure(review_id: str) -> dict:
    """Get review structure (columns, files)"""
    try:
        # Check cache first
        cache_key = f"review_structure:{review_id}"
        cached_data = await redis_client.get(cache_key)
        
        if cached_data is not None:
            return msgpack.unpackb(cached_data)
        
        supabase = get_supabase_admin()
        
        # Get columns
        columns_response = supabase.table("tabular_review_columns").select("*").eq("review_id", review_id).order("column_order").execute()
        
        # Get files
        files_response = supabase.table("tabular_review_files")\
            .select("*, files(original_filename, file_size, status)")\
            .eq("review_id", review_id)\
            .execute()
        
        structure = {
            'columns': columns_response.data,
            'files': files_response.data
        }
        
        # Cache for 2 minutes
        await redis_client.setex(cache_key, 120, msgpack.packb(structure))
        
        return structure
    except Exception as e:
        print(f"Error getting review structure: {e}")
        return {'columns': [], 'files': []}

async def get_cached_results(review_id: str) -> List[dict]:
    """Get any already processed results from cache"""
    try:
        cache_key = f"review_results:{review_id}"
        cached_results = await redis_client.get(cache_key)
        
        if cached_results is not None:
            return msgpack.unpackb(cached_results)
        
        # Fetch from database and cache
        supabase = get_supabase_admin()
        results_response = supabase.table("tabular_review_results").select("*").eq("review_id", review_id).execute()
        
        # Cache for 1 minute
        await redis_client.setex(cache_key, 60, msgpack.packb(results_response.data))
        
        return results_response.data
    except Exception as e:
        print(f"Error getting cached results: {e}")
        return []

async def get_review_user(review_id: str) -> Optional[str]:
    """Get user_id for a review"""
    try:
        # Cache this mapping in Redis
        cache_key = f"review_user:{review_id}"
        cached_user = await redis_client.get(cache_key)
        
        if cached_user is not None:
            return msgpack.unpackb(cached_user).decode('utf-8')
        
        supabase = get_supabase_admin()
        resp = supabase.table("tabular_reviews").select("user_id").eq("id", review_id).execute()
        
        if resp.data:
            user_id = resp.data[0]["user_id"]
            # Cache for 10 minutes
            await redis_client.setex(cache_key, 600, msgpack.packb(user_id.encode('utf-8')))
            return user_id
        
        return None
    except Exception as e:
        print(f"Error getting review user: {e}")
        return None

async def get_folder_files(folder_id: str, user_id: str) -> List[str]:
    """Get files from folder"""
    try:
        supabase = get_supabase_admin()
        
        files_response = supabase.table("files")\
            .select("id")\
            .eq("folder_id", folder_id)\
            .eq("user_id", user_id)\
            .eq("status", "completed")\
            .execute()
        
        return [f["id"] for f in files_response.data]
    except Exception as e:
        print(f"Error getting folder files: {e}")
        return []

async def validate_folder_access(folder_id: str, user_id: str) -> bool:
    """Validate that user has access to the folder"""
    try:
        supabase = get_supabase_admin()
        
        folder_response = supabase.table("folders")\
            .select("id")\
            .eq("id", folder_id)\
            .eq("user_id", user_id)\
            .execute()
        
        return len(folder_response.data) > 0
    except Exception as e:
        print(f"Error validating folder access: {e}")
        return False

# Note: save_review_to_db function has been integrated into create_review_async for better column ID management

# ============================================================================
# WEBSOCKET ENDPOINT
# ============================================================================

@router.websocket("/ws/{review_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    review_id: str,
    token: str
):
    # Validate token and get user_id
    user_id = await validate_ws_token(token)
    if not user_id:
        await websocket.close(code=4001, reason="Unauthorized")
        return
    
    # Verify review access
    if not await verify_review_access(review_id, user_id):
        await websocket.close(code=4004, reason="Not Found")
        return
    
    await manager.connect(websocket, user_id, review_id)
    
    try:
        # Send initial structure immediately
        structure = await get_review_structure(review_id)
        await websocket.send_bytes(msgpack.packb({
            'type': 'structure',
            'data': structure
        }))
        
        # Send any cached results
        cached_results = await get_cached_results(review_id)
        if cached_results:
            await websocket.send_bytes(msgpack.packb({
                'type': 'cached_results',
                'data': cached_results
            }))
        
        # Keep connection alive and handle ping/pong
        while True:
            try:
                # Wait for client messages with timeout
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
                
                # Handle ping/pong or other client messages
                if data == "ping":
                    await websocket.send_text("pong")
                    
            except asyncio.TimeoutError:
                # Send heartbeat
                await websocket.send_bytes(msgpack.packb({
                    'type': 'heartbeat',
                    'timestamp': datetime.utcnow().isoformat()
                }))
            
    except WebSocketDisconnect:
        manager.disconnect(websocket, user_id)
    except Exception as e:
        print(f"WebSocket error: {e}")
        manager.disconnect(websocket, user_id)

# ============================================================================
# OPTIMIZED CREATE REVIEW ENDPOINT
# ============================================================================

@router.post("/", response_model=TabularReviewResponse)
async def create_review(
    review_data: TabularReviewCreate,
    background_tasks: BackgroundTasks,
    current_user = Depends(get_current_user)
):
    """Create review with immediate processing"""
    try:
        review_id = str(uuid.uuid4())
        
        print(f"🎯 Creating new tabular review: {review_data.name} (scope: {review_data.review_scope})")
        
        # Quick validation
        if not review_data.name:
            raise HTTPException(400, "Name required")
        
        # Get file IDs based on scope
        file_ids = []
        if review_data.review_scope == ReviewScope.FOLDER:
            if not review_data.folder_id:
                raise HTTPException(400, "folder_id is required for folder-based reviews")
            
            if not await validate_folder_access(review_data.folder_id, current_user.id):
                raise HTTPException(404, "Folder not found or access denied")
            
            file_ids = await get_folder_files(review_data.folder_id, current_user.id)
            
            if not file_ids:
                raise HTTPException(400, "No completed files found in the selected folder")
                
        elif review_data.review_scope == ReviewScope.FILES:
            if not review_data.file_ids:
                raise HTTPException(400, "file_ids is required for file-based reviews")
            
            # Validate files
            supabase = get_supabase_admin()
            files_response = supabase.table("files").select("id, original_filename, file_size, status, folder_id").in_("id", review_data.file_ids).eq("user_id", current_user.id).execute()
            
            if len(files_response.data) != len(review_data.file_ids):
                raise HTTPException(400, "Some files not found or don't belong to user")
            
            incomplete_files = [f for f in files_response.data if f["status"] != "completed"]
            if incomplete_files:
                raise HTTPException(400, "All files must be completed before creating a review")
            
            file_ids = review_data.file_ids
        
        if not file_ids:
            raise HTTPException(400, "No files selected")
        
        # Create review in background to return fast
        background_tasks.add_task(
            create_review_async,
            review_id,
            review_data.dict(),
            file_ids,
            current_user.id
        )
        
        # Return immediately with structure
        return TabularReviewResponse(
            id=review_id,
            user_id=current_user.id,
            name=review_data.name,
            description=review_data.description,
            status="processing",
            review_scope=review_data.review_scope,
            folder_id=review_data.folder_id if review_data.review_scope == ReviewScope.FOLDER else None,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            last_processed_at=datetime.utcnow(),
            columns=[
                TabularReviewColumnResponse(
                    id=str(uuid.uuid4()),
                    column_name=column.column_name,
                    prompt=column.prompt,
                    column_order=column.column_order if column.column_order is not None else i,
                    data_type=column.data_type,
                    created_at=datetime.utcnow()
                ) for i, column in enumerate(review_data.columns)
            ],
            files=[],  # Will be populated when files are processed
            total_files=len(file_ids),
            total_columns=len(review_data.columns),
            completion_percentage=0.0
        )
        
    except Exception as e:
        raise HTTPException(500, str(e))

async def create_review_async(review_id: str, review_data: dict, file_ids: List[str], user_id: str):
    """Create review and start processing in background"""
    try:
        # Save to database first and get the actual column IDs
        supabase = get_supabase_admin()
        
        # Create the review
        review_record = {
            "id": review_id,
            "user_id": user_id,
            "name": review_data['name'],
            "description": review_data.get('description'),
            "status": "processing",
            "review_scope": review_data.get('review_scope', 'files'),
            "folder_id": review_data.get('folder_id'),
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
            "last_processed_at": datetime.utcnow().isoformat()
        }
        
        supabase.table("tabular_reviews").insert(review_record).execute()
        
        # Create columns and get the actual IDs from database
        column_records = []
        for i, column in enumerate(review_data['columns']):
            column_id = str(uuid.uuid4())
            column_records.append({
                "id": column_id,
                "review_id": review_id,
                "column_name": column['column_name'],
                "prompt": column['prompt'],
                "column_order": column.get('column_order', i),
                "data_type": column.get('data_type', 'text'),
                "created_at": datetime.utcnow().isoformat()
            })
        
        columns_response = supabase.table("tabular_review_columns").insert(column_records).execute()
        
        # Link files to review
        file_records = []
        for file_id in file_ids:
            file_records.append({
                "id": str(uuid.uuid4()),
                "review_id": review_id,
                "file_id": file_id,
                "added_at": datetime.utcnow().isoformat()
            })
        
        supabase.table("tabular_review_files").insert(file_records).execute()
        
        # Get the actual saved columns from database to ensure correct IDs
        saved_columns_response = supabase.table("tabular_review_columns").select("*").eq("review_id", review_id).execute()
        saved_columns = saved_columns_response.data
        
        # Clear any cached data for this review
        cache_keys = [
            f"review_structure:{review_id}",
            f"review_results:{review_id}",
            f"review_user:{review_id}",
            f"review_access:{user_id}:{review_id}"
        ]
        for key in cache_keys:
            await redis_client.delete(key)
        
        # Queue all cells for processing using the actual column IDs from database
        for file_id in file_ids:
            for column in saved_columns:  # Use saved columns with correct IDs
                await processor.processing_queue.put({
                    'review_id': review_id,
                    'file_id': file_id,
                    'column_id': column['id'],  # Use actual database ID
                    'prompt': column['prompt'],
                    'column_name': column['column_name'],
                    'data_type': column.get('data_type', 'text'),
                    'user_id': user_id
                })
        
        # Notify via WebSocket that processing started
        await manager.send_to_review(user_id, review_id, {
            'type': 'processing_started',
            'total_cells': len(file_ids) * len(saved_columns),
            'timestamp': datetime.utcnow().isoformat()
        })
        
        print(f"✅ Review processing started: {review_id} with {len(saved_columns)} columns")
        
    except Exception as e:
        print(f"Background review creation error: {e}")
        await manager.send_to_review(user_id, review_id, {
            'type': 'error',
            'message': str(e),
            'timestamp': datetime.utcnow().isoformat()
        })

# ============================================================================
# CRUD ENDPOINTS
# ============================================================================

@router.get("/", response_model=TabularReviewListResponse)
async def list_tabular_reviews(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status_filter: Optional[str] = Query(None),
    folder_id: Optional[str] = Query(None),
    current_user = Depends(get_current_user)
):
    """Get list of user's tabular reviews with filtering and pagination"""
    try:
        supabase = get_supabase_admin()
        
        # Build query
        query = supabase.table("tabular_reviews").select("*", count="exact").eq("user_id", current_user.id)
        
        # Apply filters
        if status_filter:
            query = query.eq("status", status_filter)
        if folder_id:
            query = query.eq("folder_id", folder_id)
        
        # Get total count
        count_response = query.execute()
        total_count = count_response.count or 0
        
        # Get reviews with pagination
        offset = (page - 1) * page_size
        reviews_response = supabase.table("tabular_reviews")\
            .select("*")\
            .eq("user_id", current_user.id)\
            .order("created_at", desc=True)\
            .range(offset, offset + page_size - 1)
        
        # Apply same filters to paginated query
        if status_filter:
            reviews_response = reviews_response.eq("status", status_filter)
        if folder_id:
            reviews_response = reviews_response.eq("folder_id", folder_id)
        
        reviews_data = reviews_response.execute()
        
        reviews = []
        for review_data in reviews_data.data:
            # Get columns count
            columns_response = supabase.table("tabular_review_columns").select("id", count="exact").eq("review_id", review_data["id"]).execute()
            
            # Get files count
            files_response = supabase.table("tabular_review_files").select("file_id").eq("review_id", review_data["id"]).execute()
            
            # Get results count for completion percentage
            results_response = supabase.table("tabular_review_results").select("id", count="exact").eq("review_id", review_data["id"]).execute()
            
            total_possible_results = len(files_response.data) * (columns_response.count or 0)
            completion_percentage = (results_response.count / total_possible_results * 100) if total_possible_results > 0 else 0
            
            # Parse dates
            created_at = parse_iso_datetime(review_data["created_at"])
            updated_at = parse_iso_datetime(review_data["updated_at"])
            last_processed_at = None
            if review_data.get("last_processed_at"):
                last_processed_at = parse_iso_datetime(review_data["last_processed_at"])
            
            reviews.append(TabularReviewResponse(
                id=review_data["id"],
                user_id=review_data["user_id"],
                name=review_data["name"],
                description=review_data.get("description"),
                status=review_data["status"],
                review_scope=review_data.get("review_scope") or "files",
                folder_id=review_data.get("folder_id"),
                created_at=created_at,
                updated_at=updated_at,
                last_processed_at=last_processed_at,
                columns=[],  # Empty for list view
                files=[],    # Empty for list view
                total_files=len(files_response.data),
                total_columns=columns_response.count or 0,
                completion_percentage=completion_percentage
            ))
        
        total_pages = (total_count + page_size - 1) // page_size
        
        return TabularReviewListResponse(
            reviews=reviews,
            total_count=total_count,
            page=page,
            page_size=page_size,
            total_pages=total_pages
        )
        
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch reviews: {str(e)}")

@router.get("/{review_id}", response_model=TabularReviewDetailResponse)
async def get_tabular_review(
    review_id: str,
    include_results: bool = Query(True),
    current_user = Depends(get_current_user)
):
    """Get detailed tabular review with optional results"""
    try:
        supabase = get_supabase_admin()
        
        # Get review
        review_response = supabase.table("tabular_reviews").select("*").eq("id", review_id).eq("user_id", current_user.id).execute()
        
        if not review_response.data:
            raise HTTPException(404, "Review not found")
        
        review_data = review_response.data[0]
        
        # Get columns
        columns_response = supabase.table("tabular_review_columns").select("*").eq("review_id", review_id).order("column_order").execute()
        
        # Get files with details
        files_response = supabase.table("tabular_review_files")\
            .select("*, files(original_filename, file_size, status)")\
            .eq("review_id", review_id)\
            .execute()
        
        # Get results if requested
        results = []
        if include_results:
            results_response = supabase.table("tabular_review_results").select("*").eq("review_id", review_id).execute()
            
            for result_data in results_response.data:
                result_created_at = parse_iso_datetime(result_data["created_at"])
                results.append(TabularReviewResultResponse(
                    id=result_data["id"],
                    file_id=result_data["file_id"],
                    column_id=result_data["column_id"],
                    extracted_value=result_data.get("extracted_value"),
                    confidence_score=result_data.get("confidence_score"),
                    source_reference=result_data.get("source_reference"),
                    created_at=result_created_at
                ))
        
        # Parse dates
        created_at = parse_iso_datetime(review_data["created_at"])
        updated_at = parse_iso_datetime(review_data["updated_at"])
        last_processed_at = None
        if review_data.get("last_processed_at"):
            last_processed_at = parse_iso_datetime(review_data["last_processed_at"])
        
        # Build response
        columns = []
        for col_data in columns_response.data:
            col_created_at = parse_iso_datetime(col_data["created_at"])
            columns.append(TabularReviewColumnResponse(
                id=col_data["id"],
                column_name=col_data["column_name"],
                prompt=col_data["prompt"],
                column_order=col_data["column_order"],
                data_type=col_data["data_type"],
                created_at=col_created_at
            ))
        
        files = []
        for file_data in files_response.data:
            file_added_at = parse_iso_datetime(file_data["added_at"])
            files.append(TabularReviewFileResponse(
                id=file_data["id"],
                file_id=file_data["file_id"],
                filename=file_data["files"]["original_filename"],
                file_size=file_data["files"]["file_size"],
                status=file_data["files"]["status"],
                added_at=file_added_at
            ))
        
        total_possible_results = len(files) * len(columns)
        completion_percentage = (len(results) / total_possible_results * 100) if total_possible_results > 0 else 0
        
        return TabularReviewDetailResponse(
            id=review_data["id"],
            user_id=review_data["user_id"],
            name=review_data["name"],
            description=review_data.get("description"),
            status=review_data["status"],
            review_scope=review_data.get("review_scope") or "files",
            folder_id=review_data.get("folder_id"),
            created_at=created_at,
            updated_at=updated_at,
            last_processed_at=last_processed_at,
            columns=columns,
            files=files,
            total_files=len(files),
            total_columns=len(columns),
            completion_percentage=completion_percentage,
            results=results
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to fetch review: {str(e)}")

@router.post("/{review_id}/files")
async def add_files_to_review(
    review_id: str,
    request: AddFilesToReviewRequest,
    background_tasks: BackgroundTasks,
    current_user = Depends(get_current_user)
):
    """Add files to an existing review (only for file-based reviews)"""
    try:
        supabase = get_supabase_admin()
        
        print(f"🎯 Adding {len(request.file_ids)} files to review {review_id}")
        
        # Verify review belongs to user and is file-based
        review_response = supabase.table("tabular_reviews")\
            .select("id, status, review_scope")\
            .eq("id", review_id)\
            .eq("user_id", current_user.id)\
            .execute()
        
        if not review_response.data:
            raise HTTPException(404, "Review not found")
        
        review_data = review_response.data[0]
        
        if review_data["review_scope"] == "folder":
            raise HTTPException(400, "Cannot manually add files to folder-based review")
        
        # Verify files belong to user and are completed
        files_response = supabase.table("files")\
            .select("id, original_filename, file_size, status")\
            .in_("id", request.file_ids)\
            .eq("user_id", current_user.id)\
            .execute()
        
        if len(files_response.data) != len(request.file_ids):
            raise HTTPException(400, "Some files not found or don't belong to user")
        
        # Check if all files are completed
        incomplete_files = [f for f in files_response.data if f["status"] != "completed"]
        if incomplete_files:
            raise HTTPException(400, "All files must be completed before adding to review")
        
        # Check for existing files in review
        existing_response = supabase.table("tabular_review_files")\
            .select("file_id")\
            .eq("review_id", review_id)\
            .in_("file_id", request.file_ids)\
            .execute()
        
        existing_file_ids = [f['file_id'] for f in existing_response.data]
        new_file_ids = [fid for fid in request.file_ids if fid not in existing_file_ids]
        
        if not new_file_ids:
            raise HTTPException(400, "All files are already in this review")
        
        # Add new files
        file_records = []
        for file_id in new_file_ids:
            file_records.append({
                "id": str(uuid.uuid4()),
                "review_id": review_id,
                "file_id": file_id,
                "added_at": datetime.utcnow().isoformat()
            })
        
        insert_response = supabase.table("tabular_review_files").insert(file_records).execute()
        
        # Update review status and timestamp
        supabase.table("tabular_reviews").update({
            "status": "processing",
            "updated_at": datetime.utcnow().isoformat(),
            "last_processed_at": datetime.utcnow().isoformat()
        }).eq("id", review_id).execute()
        
        # Clear cache
        await redis_client.delete(f"review_structure:{review_id}")
        
        # Send immediate WebSocket notification
        await manager.send_to_review(str(current_user.id), review_id, {
            'type': 'files_added',
            'file_count': len(new_file_ids),
            'message': f'{len(new_file_ids)} documents added. Starting analysis...',
            'timestamp': datetime.utcnow().isoformat()
        })
        
        # Start analysis for new files immediately
        background_tasks.add_task(
            analyze_new_files_immediate,
            review_id,
            new_file_ids,
            str(current_user.id)
        )
        
        return {"message": f"Added {len(new_file_ids)} files to review", "added_files": len(new_file_ids)}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to add files: {str(e)}")

async def analyze_new_files_immediate(review_id: str, file_ids: List[str], user_id: str):
    """Analyze new files for all existing columns with immediate cell-by-cell updates"""
    try:
        supabase = get_supabase_admin()
        
        print(f"🚀 Starting immediate files analysis for {len(file_ids)} files")
        
        # Send start message
        await manager.send_to_review(user_id, review_id, {
            'type': 'files_analysis_started',
            'file_ids': file_ids,
            'message': f'Starting analysis for {len(file_ids)} new documents...',
            'timestamp': datetime.utcnow().isoformat()
        })
        
        # Get all columns in the review from database (not from memory)
        columns_response = supabase.table("tabular_review_columns").select("*").eq("review_id", review_id).order("column_order").execute()
        
        # Queue processing for all cells using actual database column IDs
        for file_id in file_ids:
            for column in columns_response.data:
                await processor.processing_queue.put({
                    'review_id': review_id,
                    'file_id': file_id,
                    'column_id': column['id'],  # Use actual database ID
                    'prompt': column['prompt'],
                    'column_name': column['column_name'],
                    'data_type': column.get('data_type', 'text'),
                    'user_id': user_id
                })

        
        print(f"🎉 Queued {len(file_ids) * len(columns_response.data)} cells for processing")
        
        # Send files completion message
        await manager.send_to_review(user_id, review_id, {
            'type': 'files_analysis_queued',
            'file_ids': file_ids,
            'total_cells': len(file_ids) * len(columns_response.data),
            'message': f'Analysis queued for {len(file_ids)} new documents!',
            'timestamp': datetime.utcnow().isoformat()
        })
        
    except Exception as e:
        print(f"💥 Files analysis failed for review {review_id}: {str(e)}")
        
        # Send error message
        await manager.send_to_review(user_id, review_id, {
            'type': 'files_analysis_failed',
            'file_ids': file_ids,
            'error': str(e),
            'message': 'Files analysis failed. Please try again.',
            'timestamp': datetime.utcnow().isoformat()
        })

@router.post("/{review_id}/columns")
async def add_column_to_review(
    review_id: str,
    request: AddColumnToReviewRequest,
    background_tasks: BackgroundTasks,
    current_user = Depends(get_current_user)
):
    """Add a new column to existing review and analyze for all files"""
    try:
        supabase = get_supabase_admin()
        
        print(f"🎯 Adding column to review {review_id}: {request.column_name}")
        
        # Verify review belongs to user
        review_response = supabase.table("tabular_reviews").select("id").eq("id", review_id).eq("user_id", current_user.id).execute()
        if not review_response.data:
            raise HTTPException(404, "Review not found")
        
        # Get current max order
        existing_columns = supabase.table("tabular_review_columns").select("column_order").eq("review_id", review_id).execute()
        max_order = max([col["column_order"] for col in existing_columns.data], default=-1)
        
        # Create new column
        column_id = str(uuid.uuid4())
        column_record = {
            "id": column_id,
            "review_id": review_id,
            "column_name": request.column_name,
            "prompt": request.prompt,
            "column_order": max_order + 1,
            "data_type": request.data_type,
            "created_at": datetime.utcnow().isoformat()
        }
        
        # Insert column
        column_response = supabase.table("tabular_review_columns").insert(column_record).execute()
        
        # Update review status to processing
        supabase.table("tabular_reviews").update({
            "status": "processing",
            "last_processed_at": datetime.utcnow().isoformat()
        }).eq("id", review_id).execute()
        
        # Clear cache
        await redis_client.delete(f"review_structure:{review_id}")
        
        # Send immediate WebSocket notification that column was added
        await manager.send_to_review(str(current_user.id), review_id, {
            'type': 'column_added',
            'column_id': column_id,
            'message': f'Column "{request.column_name}" added. Starting analysis...',
            'timestamp': datetime.utcnow().isoformat()
        })
        
        # Start analysis immediately
        background_tasks.add_task(
            analyze_new_column_immediate,
            review_id,
            column_id,
            str(current_user.id)
        )
        
        return column_record
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to add column: {str(e)}")

async def analyze_new_column_immediate(review_id: str, column_id: str, user_id: str):
    """Analyze new column for all existing files with immediate cell-by-cell updates"""
    try:
        supabase = get_supabase_admin()
        
        print(f"🚀 Starting immediate column analysis for {column_id}")
        
        # Send start message
        await manager.send_to_review(user_id, review_id, {
            'type': 'column_analysis_started',
            'column_id': column_id,
            'message': 'Starting analysis for new column...',
            'timestamp': datetime.utcnow().isoformat()
        })
        
        # Get column details from database to ensure we have the correct ID
        column_response = supabase.table("tabular_review_columns").select("*").eq("id", column_id).execute()
        if not column_response.data:
            print(f"❌ Column {column_id} not found in database")
            return
        
        column = column_response.data[0]
        
        # Get all files in the review
        files_response = supabase.table("tabular_review_files").select("file_id").eq("review_id", review_id).execute()
        
        # Queue processing for all cells using the actual database column ID
        for file_data in files_response.data:
            await processor.processing_queue.put({
                'review_id': review_id,
                'file_id': file_data["file_id"],
                'column_id': column['id'],  # Use verified database ID
                'prompt': column['prompt'],
                'column_name': column['column_name'],
                'data_type': column.get('data_type', 'text'),
                'user_id': user_id
            })

        
        print(f"🎉 Queued {len(files_response.data)} cells for column analysis using column ID: {column['id']}")
        
        # Send column completion message
        await manager.send_to_review(user_id, review_id, {
            'type': 'column_analysis_queued',
            'column_id': column_id,
            'total_cells': len(files_response.data),
            'message': f'Column "{column["column_name"]}" analysis queued!',
            'timestamp': datetime.utcnow().isoformat()
        })
        
    except Exception as e:
        print(f"💥 Column analysis failed for review {review_id}, column {column_id}: {str(e)}")
        
        # Send error message
        await manager.send_to_review(user_id, review_id, {
            'type': 'column_analysis_failed',
            'column_id': column_id,
            'error': str(e),
            'message': 'Column analysis failed. Please try again.',
            'timestamp': datetime.utcnow().isoformat()
        })

@router.put("/{review_id}/columns/{column_id}")
async def update_column(
    review_id: str,
    column_id: str,
    request: TabularReviewColumnUpdate,
    background_tasks: BackgroundTasks,
    current_user = Depends(get_current_user)
):
    """Update a column"""
    try:
        supabase = get_supabase_admin()
        
        # Verify review and column belong to user
        review_response = supabase.table("tabular_reviews").select("id").eq("id", review_id).eq("user_id", current_user.id).execute()
        if not review_response.data:
            raise HTTPException(404, "Review not found")
        
        column_response = supabase.table("tabular_review_columns").select("*").eq("id", column_id).eq("review_id", review_id).execute()
        if not column_response.data:
            raise HTTPException(404, "Column not found")
        
        # Prepare update data
        update_data = {}
        if request.column_name is not None:
            update_data["column_name"] = request.column_name
        if request.prompt is not None:
            update_data["prompt"] = request.prompt
        if request.data_type is not None:
            update_data["data_type"] = request.data_type
        if request.column_order is not None:
            update_data["column_order"] = request.column_order
        
        if not update_data:
            raise HTTPException(400, "No fields to update")
        
        # Update column
        update_response = supabase.table("tabular_review_columns").update(update_data).eq("id", column_id).execute()
        
        # Clear cache
        await redis_client.delete(f"review_structure:{review_id}")
        
        # If prompt was changed, trigger re-analysis
        if request.prompt is not None:
            # Clear existing results for this column
            supabase.table("tabular_review_results").delete().eq("review_id", review_id).eq("column_id", column_id).execute()
            
            # Update review status
            supabase.table("tabular_reviews").update({
                "status": "processing",
                "last_processed_at": datetime.utcnow().isoformat()
            }).eq("id", review_id).execute()
            
            # Send notification
            await manager.send_to_review(str(current_user.id), review_id, {
                'type': 'column_updated',
                'column_id': column_id,
                'message': f'Column updated. Re-analyzing...',
                'timestamp': datetime.utcnow().isoformat()
            })
            
            # Start re-analysis
            background_tasks.add_task(
                analyze_new_column_immediate,
                review_id,
                column_id,
                str(current_user.id)
            )
        
        return update_response.data[0]
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to update column: {str(e)}")

@router.delete("/{review_id}/columns/{column_id}")
async def delete_column(
    review_id: str,
    column_id: str,
    current_user = Depends(get_current_user)
):
    """Delete a column from review"""
    try:
        supabase = get_supabase_admin()
        
        # Verify review and column belong to user
        review_response = supabase.table("tabular_reviews").select("id").eq("id", review_id).eq("user_id", current_user.id).execute()
        if not review_response.data:
            raise HTTPException(404, "Review not found")
        
        column_response = supabase.table("tabular_review_columns").select("*").eq("id", column_id).eq("review_id", review_id).execute()
        if not column_response.data:
            raise HTTPException(404, "Column not found")
        
        # Delete column (cascade will delete results)
        delete_response = supabase.table("tabular_review_columns").delete().eq("id", column_id).execute()
        
        # Clear cache
        await redis_client.delete(f"review_structure:{review_id}")
        
        # Send notification
        await manager.send_to_review(str(current_user.id), review_id, {
            'type': 'column_deleted',
            'column_id': column_id,
            'message': 'Column deleted successfully',
            'timestamp': datetime.utcnow().isoformat()
        })
        
        return {"message": "Column deleted successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to delete column: {str(e)}")

@router.post("/{review_id}/analyze")
async def start_analysis(
    review_id: str,
    background_tasks: BackgroundTasks,
    analysis_request: Optional[AnalysisRequest] = None,
    current_user = Depends(get_current_user)
):
    """Start or restart AI analysis for a tabular review"""
    try:
        supabase = get_supabase_admin()
        
        print(f"🎯 Manual analysis start for review {review_id}")
        
        # Verify review belongs to user
        review_response = supabase.table("tabular_reviews").select("id, status").eq("id", review_id).eq("user_id", current_user.id).execute()
        
        if not review_response.data:
            raise HTTPException(404, "Review not found")
        
        # Clear existing results if force reprocess
        if analysis_request and analysis_request.force_reprocess:
            supabase.table("tabular_review_results").delete().eq("review_id", review_id).execute()
            # Clear cache
            await redis_client.delete(f"review_results:{review_id}")
        
        # Update review status to processing
        supabase.table("tabular_reviews").update({
            "status": "processing",
            "last_processed_at": datetime.utcnow().isoformat()
        }).eq("id", review_id).execute()
        
        # Start immediate analysis
        background_tasks.add_task(
            process_review_analysis_realtime,
            review_id,
            str(current_user.id)
        )
        
        return {"message": "Analysis started", "review_id": review_id, "status": "processing"}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to start analysis: {str(e)}")

async def process_review_analysis_realtime(review_id: str, user_id: str):
    """Process complete review analysis with immediate real-time updates"""
    try:
        supabase = get_supabase_admin()
        
        print(f"🚀 Starting immediate review analysis for {review_id}")
        
        # Send start message
        await manager.send_to_review(user_id, review_id, {
            'type': 'analysis_started',
            'message': 'Starting document analysis...',
            'timestamp': datetime.utcnow().isoformat()
        })
        
        # Get review details
        review_response = supabase.table("tabular_reviews").select("*").eq("id", review_id).execute()
        if not review_response.data:
            return
        
        # Get columns and files from database to ensure correct IDs
        columns_response = supabase.table("tabular_review_columns").select("*").eq("review_id", review_id).order("column_order").execute()
        files_response = supabase.table("tabular_review_files").select("file_id").eq("review_id", review_id).execute()
        
        total_tasks = len(files_response.data) * len(columns_response.data)
        
        print(f"📊 Total cells to process: {total_tasks}")
        
        # Queue all cells for processing using actual database column IDs
        for file_data in files_response.data:
            for column in columns_response.data:
                await processor.processing_queue.put({
                    'review_id': review_id,
                    'file_id': file_data["file_id"],
                    'column_id': column['id'],  # Use actual database ID
                    'prompt': column['prompt'],
                    'column_name': column['column_name'],
                    'data_type': column.get('data_type', 'text'),
                    'user_id': user_id
                })
        
        # Send processing queued message
        await manager.send_to_review(user_id, review_id, {
            'type': 'analysis_queued',
            'total_cells': total_tasks,
            'message': f'Analysis queued for {total_tasks} cells',
            'timestamp': datetime.utcnow().isoformat()
        })
        
        print(f"🎉 Analysis queued for review {review_id} with {len(columns_response.data)} columns")
        
    except Exception as e:
        print(f"💥 Analysis failed for review {review_id}: {str(e)}")
        
        # Update review status to failed
        supabase = get_supabase_admin()
        supabase.table("tabular_reviews").update({
            "status": "failed",
            "last_processed_at": datetime.utcnow().isoformat()
        }).eq("id", review_id).execute()
        
        # Send error message
        await manager.send_to_review(user_id, review_id, {
            'type': 'analysis_failed',
            'error': str(e),
            'message': 'Analysis failed. Please try again.',
            'timestamp': datetime.utcnow().isoformat()
        })

@router.get("/{review_id}/status", response_model=AnalysisStatus)
async def get_analysis_status(
    review_id: str,
    current_user = Depends(get_current_user)
):
    """Get the current status of an analysis"""
    try:
        supabase = get_supabase_admin()
        
        # Get review
        review_response = supabase.table("tabular_reviews").select("*").eq("id", review_id).eq("user_id", current_user.id).execute()
        
        if not review_response.data:
            raise HTTPException(404, "Review not found")
        
        review = review_response.data[0]
        
        # Get progress information
        files_count_response = supabase.table("tabular_review_files").select("id", count="exact").eq("review_id", review_id).execute()
        columns_count_response = supabase.table("tabular_review_columns").select("id", count="exact").eq("review_id", review_id).execute()
        results_count_response = supabase.table("tabular_review_results").select("id", count="exact").eq("review_id", review_id).execute()
        
        total_files = files_count_response.count or 0
        total_columns = columns_count_response.count or 0
        total_cells = total_files * total_columns
        completed_cells = results_count_response.count or 0
        
        progress_percentage = (completed_cells / total_cells * 100) if total_cells > 0 else 0
        files_processed = completed_cells // total_columns if total_columns > 0 else 0
        
        return AnalysisStatus(
            review_id=review_id,
            status=review["status"],
            progress_percentage=progress_percentage,
            files_processed=files_processed,
            total_files=total_files,
            cells_completed=completed_cells,
            total_cells=total_cells,
            estimated_completion=None,
            error_message=None,
            current_task=f"Processing {files_processed}/{total_files} files" if review["status"] == "processing" else None
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to get analysis status: {str(e)}")

@router.put("/{review_id}/results/{result_id}")
async def update_result(
    review_id: str,
    result_id: str,
    request: TabularReviewResultUpdate,
    current_user = Depends(get_current_user)
):
    """Update a specific result (for manual corrections)"""
    try:
        supabase = get_supabase_admin()
        
        # Verify review and result belong to user
        review_response = supabase.table("tabular_reviews").select("id").eq("id", review_id).eq("user_id", current_user.id).execute()
        if not review_response.data:
            raise HTTPException(404, "Review not found")
        
        result_response = supabase.table("tabular_review_results").select("*").eq("id", result_id).eq("review_id", review_id).execute()
        if not result_response.data:
            raise HTTPException(404, "Result not found")
        
        # Prepare update data
        update_data = {
            "updated_at": datetime.utcnow().isoformat()
        }
        
        if request.extracted_value is not None:
            update_data["extracted_value"] = request.extracted_value
        if request.confidence_score is not None:
            update_data["confidence_score"] = request.confidence_score
        if request.source_reference is not None:
            update_data["source_reference"] = request.source_reference
        
        # Update result
        update_response = supabase.table("tabular_review_results").update(update_data).eq("id", result_id).execute()
        
        # Clear cache
        await redis_client.delete(f"review_results:{review_id}")
        
        # Send real-time update
        await manager.send_to_review(str(current_user.id), review_id, {
            'type': 'result_updated',
            'result_id': result_id,
            'message': 'Result updated successfully',
            'timestamp': datetime.utcnow().isoformat()
        })
        
        return update_response.data[0]
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to update result: {str(e)}")

@router.get("/{review_id}/export")
async def export_review_data(
    review_id: str,
    format: ExportFormat = Query(ExportFormat.CSV),
    include_metadata: bool = Query(True),
    include_confidence: bool = Query(True),
    current_user = Depends(get_current_user)
):
    """Export review data in various formats"""
    try:
        supabase = get_supabase_admin()
        
        # Verify review belongs to user
        review_response = supabase.table("tabular_reviews").select("*").eq("id", review_id).eq("user_id", current_user.id).execute()
        if not review_response.data:
            raise HTTPException(404, "Review not found")
        
        review = review_response.data[0]
        
        # Get columns, files, and results
        columns_response = supabase.table("tabular_review_columns").select("*").eq("review_id", review_id).order("column_order").execute()
        files_response = supabase.table("tabular_review_files").select("*, files(original_filename)").eq("review_id", review_id).execute()
        results_response = supabase.table("tabular_review_results").select("*").eq("review_id", review_id).execute()
        
        # Build results matrix
        results_dict = {}
        for result in results_response.data:
            key = (result["file_id"], result["column_id"])
            results_dict[key] = result
        
        if format == ExportFormat.CSV:
            # Create CSV
            output = io.StringIO()
            writer = csv.writer(output)
            
            # Header row
            header = ["Document"]
            for column in columns_response.data:
                header.append(column["column_name"])
                if include_confidence:
                    header.append(f"{column['column_name']}_confidence")
                if include_metadata:
                    header.append(f"{column['column_name']}_source")
            
            writer.writerow(header)
            
            # Data rows
            for file_data in files_response.data:
                row = [file_data["files"]["original_filename"]]
                
                for column in columns_response.data:
                    key = (file_data["file_id"], column["id"])
                    result = results_dict.get(key)
                    
                    # Value
                    row.append(result["extracted_value"] if result else "")
                    
                    # Confidence
                    if include_confidence:
                        row.append(result["confidence_score"] if result else "")
                    
                    # Source
                    if include_metadata:
                        row.append(result["source_reference"] if result else "")
                
                writer.writerow(row)
            
            output.seek(0)
            content = output.getvalue()
            
            return Response(
                content=content,
                media_type="text/csv",
                headers={"Content-Disposition": f"attachment; filename={review['name']}_export.csv"}
            )
        
        elif format == ExportFormat.JSON:
            # Create JSON structure
            export_data = {
                "review": {
                    "id": review["id"],
                    "name": review["name"],
                    "description": review.get("description"),
                    "created_at": review["created_at"],
                    "total_files": len(files_response.data),
                    "total_columns": len(columns_response.data)
                },
                "columns": [
                    {
                        "id": col["id"],
                        "name": col["column_name"],
                        "prompt": col["prompt"],
                        "data_type": col["data_type"],
                        "order": col["column_order"]
                    } for col in columns_response.data
                ],
                "data": []
            }
            
            for file_data in files_response.data:
                file_results = {
                    "file_id": file_data["file_id"],
                    "filename": file_data["files"]["original_filename"],
                    "results": {}
                }
                
                for column in columns_response.data:
                    key = (file_data["file_id"], column["id"])
                    result = results_dict.get(key)
                    
                    column_result = {
                        "value": result["extracted_value"] if result else None
                    }
                    
                    if include_confidence:
                        column_result["confidence"] = result["confidence_score"] if result else None
                    
                    if include_metadata:
                        column_result["source"] = result["source_reference"] if result else None
                    
                    file_results["results"][column["column_name"]] = column_result
                
                export_data["data"].append(file_results)
            
            return Response(
                content=json.dumps(export_data, indent=2),
                media_type="application/json",
                headers={"Content-Disposition": f"attachment; filename={review['name']}_export.json"}
            )
        
        else:
            raise HTTPException(400, "Unsupported export format")
            
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to export data: {str(e)}")

@router.get("/{review_id}/stats", response_model=ValidationStats)
async def get_review_stats(
    review_id: str,
    current_user = Depends(get_current_user)
):
    """Get validation and quality statistics for a review"""
    try:
        supabase = get_supabase_admin()
        
        # Verify review belongs to user
        review_response = supabase.table("tabular_reviews").select("id").eq("id", review_id).eq("user_id", current_user.id).execute()
        if not review_response.data:
            raise HTTPException(404, "Review not found")
        
        # Get all results for statistics
        results_response = supabase.table("tabular_review_results").select("*").eq("review_id", review_id).execute()
        
        total_results = len(results_response.data)
        validated_results = 0  # No manual editing field in database
        invalid_results = 0    # No invalid field in database
        high_confidence_results = len([r for r in results_response.data if (r.get("confidence_score") or 0) > 0.8])
        low_confidence_results = len([r for r in results_response.data if (r.get("confidence_score") or 0) < 0.5])
        manually_edited_results = 0  # No manual editing field in database
        
        return ValidationStats(
            total_results=total_results,
            validated_results=validated_results,
            invalid_results=invalid_results,
            high_confidence_results=high_confidence_results,
            low_confidence_results=low_confidence_results,
            manually_edited_results=manually_edited_results
        )
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to get stats: {str(e)}")

@router.delete("/{review_id}")
async def delete_tabular_review(
    review_id: str,
    current_user = Depends(get_current_user)
):
    """Delete a tabular review"""
    try:
        supabase = get_supabase_admin()
        
        # Verify review belongs to user
        review_response = supabase.table("tabular_reviews").select("id").eq("id", review_id).eq("user_id", current_user.id).execute()
        
        if not review_response.data:
            raise HTTPException(404, "Review not found")
        
        # Delete review (cascading deletes will handle related records)
        supabase.table("tabular_reviews").delete().eq("id", review_id).execute()
        
        # Clean up cache
        cache_keys = [
            f"review_structure:{review_id}",
            f"review_results:{review_id}",
            f"review_user:{review_id}",
            f"review_access:{current_user.id}:{review_id}"
        ]
        for key in cache_keys:
            await redis_client.delete(key)
        
        print(f"🗑️  Cleaned up cache for deleted review: {review_id}")
        
        return {"message": "Review deleted successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to delete review: {str(e)}")

@router.get("/summary", response_model=ReviewSummary)
async def get_reviews_summary(
    current_user = Depends(get_current_user)
):
    """Get summary statistics for all user reviews"""
    try:
        supabase = get_supabase_admin()
        
        # Get review counts by status
        reviews_response = supabase.table("tabular_reviews").select("status").eq("user_id", current_user.id).execute()
        
        total_reviews = len(reviews_response.data)
        active_reviews = len([r for r in reviews_response.data if r["status"] == "processing"])
        completed_reviews = len([r for r in reviews_response.data if r["status"] == "completed"])
        failed_reviews = len([r for r in reviews_response.data if r["status"] == "failed"])
        
        # Get document and extraction counts (simplified for performance)
        files_response = supabase.table("tabular_review_files").select("review_id").execute()
        results_response = supabase.table("tabular_review_results").select("confidence_score").execute()
        
        # Filter for user's reviews
        user_review_ids = [r["id"] for r in reviews_response.data]
        user_files = [f for f in files_response.data if f["review_id"] in user_review_ids]
        user_results = [r for r in results_response.data if any(f["review_id"] == r.get("review_id") for f in user_files)]
        
        total_documents_processed = len(user_files)
        total_extractions = len(user_results)
        
        # Calculate average confidence
        confidences = [r["confidence_score"] for r in user_results if r.get("confidence_score") is not None]
        average_confidence = sum(confidences) / len(confidences) if confidences else 0.0
        
        return ReviewSummary(
            total_reviews=total_reviews,
            active_reviews=active_reviews,
            completed_reviews=completed_reviews,
            failed_reviews=failed_reviews,
            total_documents_processed=total_documents_processed,
            total_extractions=total_extractions,
            average_confidence=average_confidence
        )
        
    except Exception as e:
        raise HTTPException(500, f"Failed to get summary: {str(e)}")

# ============================================================================
# BACKGROUND CLEANUP TASKS
# ============================================================================

async def cleanup_old_cache():
    """Clean up old cached data"""
    while True:
        try:
            await asyncio.sleep(3600)  # Clean every hour
            
            # Get all cache keys
            keys = await redis_client.keys("*")
            
            # Clean up old review-specific caches
            current_time = time.time()
            for key in keys:
                if key.startswith(b"review_"):
                    try:
                        ttl = await redis_client.ttl(key)
                        if ttl < 0:  # No expiration set
                            await redis_client.expire(key, 3600)  # Set 1 hour expiration
                    except:
                        pass
                    
            print("🧹 Completed cache cleanup")
                    
        except Exception as e:
            print(f"💥 Error in cache cleanup: {e}")
        except asyncio.CancelledError:
            break

print("🚀")

