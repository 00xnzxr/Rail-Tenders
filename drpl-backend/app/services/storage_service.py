"""
DRPL Backend - Storage Service
Unified file storage abstraction with local filesystem and Cloudflare R2 backends.

All writes/reads go through this service. The DB stores object keys (relative
paths) in `file_path` columns — never absolute filesystem paths. In R2 mode,
keys map directly to objects in the bucket. In local mode, keys resolve to
files under `settings.upload_dir`.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import shutil
import tempfile
from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator, Optional

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class StorageService:
    """File storage with local or R2 backend. Methods are backend-agnostic."""

    def __init__(self, settings):
        self._settings = settings
        self._backend = (settings.storage_backend or "local").lower()
        self._client = None
        self._bucket = settings.r2_bucket_name

        if self._backend == "r2":
            endpoint = settings.r2_endpoint_url or (
                f"https://{settings.r2_account_id}.r2.cloudflarestorage.com"
            )
            if not settings.r2_access_key_id or not settings.r2_secret_access_key:
                raise RuntimeError(
                    "storage_backend=r2 requires R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY"
                )
            self._client = boto3.client(
                "s3",
                endpoint_url=endpoint,
                aws_access_key_id=settings.r2_access_key_id,
                aws_secret_access_key=settings.r2_secret_access_key,
                region_name="auto",
                config=BotoConfig(
                    signature_version="s3v4",
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            )
            logger.info(f"StorageService: R2 backend, bucket={self._bucket}")
        else:
            logger.info(f"StorageService: local backend, dir={settings.upload_dir}")

    # --- Key normalization ---

    @staticmethod
    def normalize_key(key_or_path: str) -> str:
        """Strip leading ./uploads/, /uploads/, or absolute prefixes to produce a pure key."""
        if not key_or_path:
            return ""
        k = key_or_path.replace("\\", "/")
        for prefix in ("./uploads/", "uploads/"):
            if k.startswith(prefix):
                k = k[len(prefix):]
                break
        return k.lstrip("/")

    def _local_path(self, key: str) -> str:
        """Resolve a storage key to a local filesystem path (local backend only)."""
        return os.path.join(self._settings.upload_dir, key)

    # --- Public API ---

    async def upload_file(
        self,
        key: str,
        content: bytes,
        content_type: Optional[str] = None,
    ) -> str:
        """Upload bytes under `key`. Returns the stored key."""
        key = self.normalize_key(key)
        ctype = content_type or mimetypes.guess_type(key)[0] or "application/octet-stream"

        if self._backend == "r2":
            await asyncio.to_thread(
                self._client.put_object,
                Bucket=self._bucket,
                Key=key,
                Body=content,
                ContentType=ctype,
            )
        else:
            path = self._local_path(key)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(content)
        return key

    def upload_file_sync(
        self,
        key: str,
        content: bytes,
        content_type: Optional[str] = None,
    ) -> str:
        """Sync variant for background tasks / scripts."""
        key = self.normalize_key(key)
        ctype = content_type or mimetypes.guess_type(key)[0] or "application/octet-stream"

        if self._backend == "r2":
            self._client.put_object(
                Bucket=self._bucket, Key=key, Body=content, ContentType=ctype,
            )
        else:
            path = self._local_path(key)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(content)
        return key

    async def download_file(self, key: str) -> bytes:
        """Download object bytes by key."""
        key = self.normalize_key(key)
        if self._backend == "r2":
            obj = await asyncio.to_thread(
                self._client.get_object, Bucket=self._bucket, Key=key
            )
            return obj["Body"].read()
        with open(self._local_path(key), "rb") as f:
            return f.read()

    def download_file_sync(self, key: str) -> bytes:
        """Sync variant for ZIP builds and background tasks."""
        key = self.normalize_key(key)
        if self._backend == "r2":
            obj = self._client.get_object(Bucket=self._bucket, Key=key)
            return obj["Body"].read()
        with open(self._local_path(key), "rb") as f:
            return f.read()

    async def delete_file(self, key: str) -> None:
        key = self.normalize_key(key)
        if self._backend == "r2":
            await asyncio.to_thread(
                self._client.delete_object, Bucket=self._bucket, Key=key
            )
        else:
            path = self._local_path(key)
            if os.path.exists(path):
                os.remove(path)

    def delete_file_sync(self, key: str) -> None:
        key = self.normalize_key(key)
        if self._backend == "r2":
            self._client.delete_object(Bucket=self._bucket, Key=key)
        else:
            path = self._local_path(key)
            if os.path.exists(path):
                os.remove(path)

    async def file_exists(self, key: str) -> bool:
        key = self.normalize_key(key)
        if self._backend == "r2":
            try:
                await asyncio.to_thread(
                    self._client.head_object, Bucket=self._bucket, Key=key
                )
                return True
            except ClientError:
                return False
        return os.path.exists(self._local_path(key))

    def file_exists_sync(self, key: str) -> bool:
        key = self.normalize_key(key)
        if self._backend == "r2":
            try:
                self._client.head_object(Bucket=self._bucket, Key=key)
                return True
            except ClientError:
                return False
        return os.path.exists(self._local_path(key))

    async def get_presigned_url(
        self,
        key: str,
        expires_in: Optional[int] = None,
        response_content_disposition: Optional[str] = None,
    ) -> str:
        """Return a short-lived URL for direct client download. Local backend returns a relative path."""
        key = self.normalize_key(key)
        if self._backend == "r2":
            ttl = expires_in or self._settings.presigned_url_ttl_seconds
            params = {"Bucket": self._bucket, "Key": key}
            if response_content_disposition:
                params["ResponseContentDisposition"] = response_content_disposition
            return await asyncio.to_thread(
                self._client.generate_presigned_url,
                "get_object",
                Params=params,
                ExpiresIn=ttl,
            )
        # Local: caller should use StreamingResponse instead — return the local path.
        return self._local_path(key)

    def get_presigned_url_sync(
        self,
        key: str,
        expires_in: Optional[int] = None,
        response_content_disposition: Optional[str] = None,
    ) -> Optional[str]:
        """Presign from a synchronous request handler.

        Signing is local HMAC arithmetic — botocore issues no network call — so
        a sync route can do it without the usual blocking-IO objection. Returns
        ``None`` on the local backend, where there is nothing to presign and the
        caller must fall back to streaming through the app.
        """
        if self._backend != "r2":
            return None
        key = self.normalize_key(key)
        params = {"Bucket": self._bucket, "Key": key}
        if response_content_disposition:
            params["ResponseContentDisposition"] = response_content_disposition
        return self._client.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=expires_in or self._settings.presigned_url_ttl_seconds,
        )

    @contextmanager
    def as_local_file(self, key: str, suffix: str = "") -> Iterator[str]:
        """
        Context manager yielding a local filesystem path for an object.

        In R2 mode, downloads the object to a temp file and cleans up on exit.
        In local mode, yields the existing local path (no copy).

        Use this to bridge third-party libs (python-docx, pdfplumber, etc.)
        that require a file path rather than bytes.
        """
        key = self.normalize_key(key)
        if self._backend == "local":
            local = self._local_path(key)
            if not os.path.exists(local):
                raise FileNotFoundError(f"Storage key not found: {key}")
            yield local
            return

        fd, tmp_path = tempfile.mkstemp(suffix=suffix or os.path.splitext(key)[1])
        try:
            os.close(fd)
            try:
                obj = self._client.get_object(Bucket=self._bucket, Key=key)
            except self._client.exceptions.NoSuchKey as e:
                raise FileNotFoundError(f"Storage key not found: {key}") from e
            with open(tmp_path, "wb") as out:
                shutil.copyfileobj(obj["Body"], out)
            yield tmp_path
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


@lru_cache()
def get_storage_service() -> StorageService:
    return StorageService(get_settings())


# --- Key builder helpers ------------------------------------------------------

def key_checklist_upload(tender_id: int, file_name: str) -> str:
    return f"tenders/{tender_id}/checklist/{file_name}"


def key_tender_doc(tender_id: int, file_name: str) -> str:
    return f"tenders/{tender_id}/docs/{file_name}"


def key_gem_file(tender_id: int, file_name: str) -> str:
    return f"tenders/{tender_id}/gem/{file_name}"


def key_proposal_doc(session_id: int, file_name: str) -> str:
    return f"proposals/{session_id}/{file_name}"


def key_command_center_attachment(session_id: int, file_name: str) -> str:
    return f"command_center/{session_id}/{file_name}"


def key_offline_doc(user_id: int, file_name: str) -> str:
    """Storage key for an offline-uploaded PDF and its signed output.

    Caller is responsible for making `file_name` collision-safe (e.g. a
    timestamp prefix) — mirrors the command-center attachment convention.
    """
    return f"offline_documents/{user_id}/{file_name}"
