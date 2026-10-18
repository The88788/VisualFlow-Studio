"""Storage abstraction for ImageAPI (local disk or S3-compatible)."""

import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import AsyncIterator, Optional

from ..core.config import config, DATA_DIR
from ..core.logger import logger


class StorageBackend(ABC):
    """Abstract storage backend."""

    @abstractmethod
    async def upload(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        ...

    @abstractmethod
    async def download(self, key: str) -> bytes:
        ...

    @abstractmethod
    async def delete(self, key: str) -> None:
        ...

    @abstractmethod
    async def delete_prefix(self, prefix: str) -> int:
        ...

    def public_url(self, key: str) -> Optional[str]:
        """Return public URL for a key, or None if not applicable."""
        return None

    async def close(self) -> None:
        pass


class LocalStorage(StorageBackend):
    """Local filesystem storage. Keys are relative paths under base_dir."""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir

    def _resolve(self, key: str) -> Path:
        return self.base_dir / key

    async def upload(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def download(self, key: str) -> bytes:
        path = self._resolve(key)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {key}")
        return path.read_bytes()

    async def delete(self, key: str) -> None:
        path = self._resolve(key)
        if path.is_file():
            path.unlink(missing_ok=True)

    async def delete_prefix(self, prefix: str) -> int:
        path = self._resolve(prefix)
        if path.is_dir():
            count = sum(1 for _ in path.rglob("*") if _.is_file())
            shutil.rmtree(path)
            return count
        return 0


class S3Storage(StorageBackend):
    """S3-compatible storage backend using aioboto3."""

    def __init__(
        self,
        bucket: str,
        endpoint_url: Optional[str] = None,
        access_key: str = "",
        secret_key: str = "",
        region: str = "us-east-1",
        public_url_base: Optional[str] = None,
    ):
        import aioboto3
        from botocore.config import Config as BotoConfig

        self.bucket = bucket
        self._region = region
        self._endpoint_url = endpoint_url or None
        self._public_url_base = public_url_base

        self._session = aioboto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )
        self._boto_config = BotoConfig(signature_version="s3v4")
        self._client_ctx = None
        self._client = None

    async def _get_client(self):
        if self._client is None:
            self._client_ctx = self._session.client(
                "s3",
                endpoint_url=self._endpoint_url,
                config=self._boto_config,
            )
            self._client = await self._client_ctx.__aenter__()
        return self._client

    async def upload(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        client = await self._get_client()
        await client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        logger.debug(f"S3 uploaded: {key} ({len(data)} bytes)")

    async def download(self, key: str) -> bytes:
        client = await self._get_client()
        try:
            response = await client.get_object(Bucket=self.bucket, Key=key)
            return await response["Body"].read()
        except Exception:
            raise FileNotFoundError(f"S3 object not found: {key}")

    async def delete(self, key: str) -> None:
        client = await self._get_client()
        await client.delete_object(Bucket=self.bucket, Key=key)

    async def delete_prefix(self, prefix: str) -> int:
        client = await self._get_client()
        count = 0
        paginator = client.get_paginator("list_objects_v2")
        async for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            objects = page.get("Contents", [])
            if objects:
                await client.delete_objects(
                    Bucket=self.bucket,
                    Delete={"Objects": [{"Key": obj["Key"]} for obj in objects]},
                )
                count += len(objects)
        return count

    def public_url(self, key: str) -> Optional[str]:
        if self._public_url_base:
            return f"{self._public_url_base.rstrip('/')}/{key}"
        if self._endpoint_url:
            return f"{self._endpoint_url.rstrip('/')}/{self.bucket}/{key}"
        return f"https://{self.bucket}.s3.{self._region}.amazonaws.com/{key}"

    async def close(self) -> None:
        if self._client_ctx:
            await self._client_ctx.__aexit__(None, None, None)
            self._client = None
            self._client_ctx = None


def get_storage() -> StorageBackend:
    """Factory: return S3Storage if configured, else LocalStorage."""
    if config.s3_enabled and config.s3_bucket:
        logger.info(
            f"Using S3 storage: bucket={config.s3_bucket}, "
            f"endpoint={config.s3_endpoint_url or 'AWS'}"
        )
        return S3Storage(
            bucket=config.s3_bucket,
            endpoint_url=config.s3_endpoint_url or None,
            access_key=config.s3_access_key,
            secret_key=config.s3_secret_key,
            region=config.s3_region or "us-east-1",
            public_url_base=config.s3_public_url_base or None,
        )
    logger.info("Using local filesystem storage")
    return LocalStorage(base_dir=DATA_DIR / "results")


# Global singleton — initialized lazily so config is loaded first
_storage: Optional[StorageBackend] = None


def storage() -> StorageBackend:
    global _storage
    if _storage is None:
        _storage = get_storage()
    return _storage


async def close_storage() -> None:
    global _storage
    if _storage is not None:
        await _storage.close()
        _storage = None
