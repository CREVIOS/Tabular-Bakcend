# celery_app.py - Updated version
from celery import Celery
from core.config import settings
import os

# macOS-specific multiprocessing configuration
if os.name != 'nt':  # Not Windows
    import multiprocessing
    try:
        multiprocessing.set_start_method('fork', force=True)
    except RuntimeError:
        pass  # Already set

celery_app = Celery(
    "document_processor",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["tasks.document_processor"]
)

# Enhanced configuration for better reliability
celery_app.conf.update(
    # Serialization
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    
    # Timezone
    timezone="UTC",
    enable_utc=True,
    
    # Worker configuration
    worker_prefetch_multiplier=1,  # Important for memory-intensive tasks
    task_acks_late=True,
    worker_max_tasks_per_child=10,  # Restart workers after 10 tasks to prevent memory leaks
    worker_max_memory_per_child=1024000,  # 1GB memory limit per worker
    
    # Task routing and execution
    task_default_queue='default',
    task_default_exchange='default',
    task_default_routing_key='default',
    
    # Retry configuration
    task_annotations={
        'tasks.document_processor.process_document_task': {
            'rate_limit': '2/m',  # Max 2 tasks per minute to prevent overload
            'max_retries': 3,
            'default_retry_delay': 60,
        }
    },
    
    # Connection settings
    broker_connection_retry_on_startup=True,
    broker_connection_retry=True,
    broker_connection_max_retries=10,
    
    # Result backend settings
    result_expires=3600,  # Results expire after 1 hour
    result_persistent=True,
    
    # Worker settings for macOS
    worker_disable_rate_limits=False,
    worker_hijack_root_logger=False,
    worker_log_color=True if os.getenv('CELERY_LOG_COLOR', '1') == '1' else False,
)