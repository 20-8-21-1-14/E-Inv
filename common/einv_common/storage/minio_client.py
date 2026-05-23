from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import aiobotocore.session

from einv_common.config import settings


class StorageClient:
    def __init__(self) -> None:
        self._session = aiobotocore.session.get_session()

    @asynccontextmanager
    async def _client(self):
        async with self._session.create_client(
            "s3",
            endpoint_url=f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}",
            aws_access_key_id=settings.minio_access_key,
            aws_secret_access_key=settings.minio_secret_key,
            region_name="us-east-1",
        ) as client:
            yield client

    async def upload(self, bucket: str, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        async with self._client() as client:
            await client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
        return key

    async def download(self, bucket: str, key: str) -> bytes:
        async with self._client() as client:
            response = await client.get_object(Bucket=bucket, Key=key)
            async with response["Body"] as stream:
                return await stream.read()

    async def exists(self, bucket: str, key: str) -> bool:
        async with self._client() as client:
            try:
                await client.head_object(Bucket=bucket, Key=key)
                return True
            except client.exceptions.ClientError:
                return False

    async def presigned_url(self, bucket: str, key: str, expires_in: int = 3600) -> str:
        async with self._client() as client:
            return await client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=expires_in,
            )

    async def ensure_buckets(self) -> None:
        buckets = [
            settings.minio_bucket_raw,
            settings.minio_bucket_processed,
            settings.minio_bucket_models,
            settings.minio_bucket_training,
        ]
        async with self._client() as client:
            existing = {b["Name"] for b in (await client.list_buckets())["Buckets"]}
            for bucket in buckets:
                if bucket not in existing:
                    await client.create_bucket(Bucket=bucket)


_storage_client: StorageClient | None = None


def get_storage_client() -> StorageClient:
    global _storage_client
    if _storage_client is None:
        _storage_client = StorageClient()
    return _storage_client
