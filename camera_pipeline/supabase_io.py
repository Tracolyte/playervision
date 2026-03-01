from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from supabase import create_client, Client
from supabase.client import ClientOptions


@dataclass(frozen=True)
class SupabaseHandle:
    client: Client
    bucket: str
    table: str


def init_supabase(url: str, service_role_key: str, bucket: str, table: str) -> SupabaseHandle:
    # Use explicit timeouts so transient slowness doesn't block forever.
    opts = ClientOptions(
        postgrest_client_timeout=15,
        storage_client_timeout=30,
        schema="public",
    )
    client = create_client(url, service_role_key, options=opts)
    return SupabaseHandle(client=client, bucket=bucket, table=table)


def upload_jpeg(
    sb: SupabaseHandle,
    storage_path: str,
    local_file_path: str,
    cache_control_seconds: int,
    upsert: bool,
) -> str:
    """
    Uploads a file to Supabase Storage.

    Returns the uploaded path (as reported by the SDK) or the intended path.
    """
    file_options = {
        "cache-control": str(cache_control_seconds),
        "upsert": "true" if upsert else "false",
        "content-type": "image/jpeg",
    }

    with open(local_file_path, "rb") as f:
        resp = sb.client.storage.from_(sb.bucket).upload(
            file=f,
            path=storage_path,
            file_options=file_options,
        )

    # The SDK commonly returns a response with .path. Be defensive.
    if hasattr(resp, "path") and resp.path:
        return resp.path  # type: ignore[attr-defined]

    if isinstance(resp, dict):
        return resp.get("path") or storage_path

    return storage_path


def upsert_still_row(
    sb: SupabaseHandle,
    row: Dict[str, Any],
    on_conflict: str,
) -> None:
    resp = sb.client.table(sb.table).upsert(row, on_conflict=on_conflict).execute()

    err = getattr(resp, "error", None)
    if err:
        raise RuntimeError(str(err))
