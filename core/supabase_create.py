# core/supabase_create.py
import httpx, asyncio
from supabase import create_client, Client
from core.config import settings

_pool: httpx.AsyncClient | None = None
def _get_pool() -> httpx.AsyncClient:
    global _pool
    if _pool is None:
        _pool = httpx.AsyncClient(timeout=30)
    return _pool

def _patch_sessions(sb: Client):
    hc = _get_pool()
    for attr in ("_postgrest_client", "_storage_client"):
        if hasattr(sb, attr) and hasattr(getattr(sb, attr), "_session"):
            getattr(sb, attr)._session = hc
    if hasattr(sb, "auth") and hasattr(sb.auth, "_session"):
        sb.auth._session = hc

def get_supabase_client() -> Client:
    sb = create_client(settings.supabase_url, settings.supabase_anon_key)
    _patch_sessions(sb)
    return sb



def get_supabase_admin() -> Client:             # service key
    sb = create_client(settings.supabase_url, settings.supabase_service_role_key)
    _patch_sessions(sb)
    return sb

    
