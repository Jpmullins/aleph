"""MinIO/S3-compatible asset store wrapper.

`storage_uri` follows the Inc 1 spec layout:
  s3://<bucket>/projects/{project_id}/sources/{source_id}/{sha256}.{ext}
  s3://<bucket>/projects/{project_id}/normalized/{source_id}/{version_no}.md

The wrapper verifies SHA-256 on reads. Read failures or hash mismatches
raise — never silent.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from minio import Minio


@dataclass(frozen=True)
class StoredAsset:
    storage_uri: str
    sha256: str
    size_bytes: int
    mime_type: str


class AssetStoreError(Exception):
    """Raised on storage failure or hash mismatch."""


class AssetStore:
    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = False,
        public_endpoint: str | None = None,
    ) -> None:
        ep = endpoint.replace("http://", "").replace("https://", "")
        self._client = Minio(
            endpoint=ep,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )
        # Presigned URLs are handed to the BROWSER, which can't resolve the
        # internal compose hostname (e.g. `minio:9000`). Presign against a
        # browser-reachable public endpoint when configured (e.g.
        # `localhost:9000` in dev); the SigV4 signature is computed for that
        # host, so the browser's request to it validates. Falls back to the
        # internal client when no public endpoint is set.
        if public_endpoint:
            pep = public_endpoint.replace("http://", "").replace("https://", "")
            pub_secure = public_endpoint.startswith("https://")
            # `region` is REQUIRED here: presigned_get_object otherwise makes a
            # live `_get_region` call to the (public) endpoint to discover the
            # bucket region — but the public host (localhost:9000) isn't
            # reachable from inside this container. Pinning the region keeps URL
            # generation fully offline. MinIO's default region is us-east-1.
            self._presign_client = Minio(
                endpoint=pep,
                access_key=access_key,
                secret_key=secret_key,
                secure=pub_secure,
                region="us-east-1",
            )
        else:
            self._presign_client = self._client
        self._bucket = bucket
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        if not self._client.bucket_exists(self._bucket):
            self._client.make_bucket(self._bucket)

    def _key(
        self,
        *,
        project_id: UUID,
        source_id: UUID,
        sha256: str,
        ext: str,
    ) -> str:
        ext = ext.lstrip(".")
        return f"projects/{project_id}/sources/{source_id}/{sha256}.{ext}"

    def _normalized_key(self, *, project_id: UUID, source_id: UUID, version_no: int) -> str:
        return f"projects/{project_id}/normalized/{source_id}/{version_no}.md"

    def put_source_asset(
        self,
        *,
        project_id: UUID,
        source_id: UUID,
        data: bytes,
        mime_type: str,
        extension: str,
    ) -> StoredAsset:
        sha = hashlib.sha256(data).hexdigest()
        key = self._key(
            project_id=project_id,
            source_id=source_id,
            sha256=sha,
            ext=extension,
        )
        from io import BytesIO

        self._client.put_object(
            self._bucket,
            key,
            data=BytesIO(data),
            length=len(data),
            content_type=mime_type,
        )
        return StoredAsset(
            storage_uri=f"s3://{self._bucket}/{key}",
            sha256=sha,
            size_bytes=len(data),
            mime_type=mime_type,
        )

    def put_normalized_markdown(
        self,
        *,
        project_id: UUID,
        source_id: UUID,
        version_no: int,
        markdown: str,
    ) -> str:
        key = self._normalized_key(
            project_id=project_id, source_id=source_id, version_no=version_no
        )
        from io import BytesIO

        data = markdown.encode("utf-8")
        self._client.put_object(
            self._bucket,
            key,
            data=BytesIO(data),
            length=len(data),
            content_type="text/markdown; charset=utf-8",
        )
        return f"s3://{self._bucket}/{key}"

    def get(self, storage_uri: str, *, expected_sha256: str | None = None) -> bytes:
        bucket, key = self._parse(storage_uri)
        resp = self._client.get_object(bucket, key)
        try:
            data = resp.read()
        finally:
            resp.close()
            resp.release_conn()
        if expected_sha256 is not None:
            got = hashlib.sha256(data).hexdigest()
            if got != expected_sha256:
                msg = f"sha256 mismatch on {storage_uri}: expected {expected_sha256}, got {got}"
                raise AssetStoreError(msg)
        return data

    def presigned_get_url(self, storage_uri: str, *, ttl: timedelta = timedelta(minutes=10)) -> str:
        bucket, key = self._parse(storage_uri)
        # Presign against the browser-reachable endpoint (see __init__).
        return self._presign_client.presigned_get_object(bucket, key, expires=ttl)

    @staticmethod
    def _parse(storage_uri: str) -> tuple[str, str]:
        if not storage_uri.startswith("s3://"):
            msg = f"not an s3:// uri: {storage_uri}"
            raise AssetStoreError(msg)
        rest = storage_uri[5:]
        bucket, _, key = rest.partition("/")
        if not bucket or not key:
            msg = f"invalid s3:// uri: {storage_uri}"
            raise AssetStoreError(msg)
        return bucket, key
