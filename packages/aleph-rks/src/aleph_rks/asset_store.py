"""Asset storage: protocol + `fs` (default) and `s3` backends.

Selected by `ALEPH_ASSET_BACKEND` via `create_asset_store`. `storage_uri`
schemes:

  fs://<key>            — FsAssetStore; bytes live at <ALEPH_ASSET_ROOT>/<key>
  s3://<bucket>/<key>   — S3AssetStore (any S3-compatible endpoint)

Key layout (identical across backends):
  projects/{project_id}/sources/{source_id}/{sha256}.{ext}
  projects/{project_id}/normalized/{source_id}/{version_no}.md

A backend only reads its own scheme — a `fs://` row read through the s3
backend (or vice versa) raises `AssetStoreError`; switching backends never
silently crosses stores. `get()` verifies SHA-256 when given an expected
hash. Read failures or hash mismatches raise — never silent.

Bytes reach the browser exclusively through the authenticated streaming
route (`GET /v1/projects/{pid}/assets/{kind}/{id}`); neither backend exposes
storage directly.
"""

from __future__ import annotations

import hashlib
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from collections.abc import Iterator

_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class StoredAsset:
    storage_uri: str
    sha256: str
    size_bytes: int
    mime_type: str


class AssetStoreError(Exception):
    """Raised on storage failure, hash mismatch, or backend/scheme mismatch."""


def _source_key(*, project_id: UUID, source_id: UUID, sha256: str, ext: str) -> str:
    # The extension can originate from an uploader-chosen filename; reduce it
    # to alphanumerics so a crafted value can't steer the key to another path
    # inside the root (root *escape* is separately blocked by _resolve).
    ext = re.sub(r"[^A-Za-z0-9]", "", ext)
    suffix = f".{ext}" if ext else ""
    return f"projects/{project_id}/sources/{source_id}/{sha256}{suffix}"


def _normalized_key(*, project_id: UUID, source_id: UUID, version_no: int) -> str:
    return f"projects/{project_id}/normalized/{source_id}/{version_no}.md"


def _verify_sha(data: bytes, expected_sha256: str | None, storage_uri: str) -> None:
    if expected_sha256 is None:
        return
    got = hashlib.sha256(data).hexdigest()
    if got != expected_sha256:
        msg = f"sha256 mismatch on {storage_uri}: expected {expected_sha256}, got {got}"
        raise AssetStoreError(msg)


class AssetStore(Protocol):
    """Typed storage surface for all asset bytes (sources, normalized text, renders)."""

    def put_source_asset(
        self,
        *,
        project_id: UUID,
        source_id: UUID,
        data: bytes,
        mime_type: str,
        extension: str,
    ) -> StoredAsset: ...

    def put_normalized_markdown(
        self,
        *,
        project_id: UUID,
        source_id: UUID,
        version_no: int,
        markdown: str,
    ) -> str: ...

    def put_bytes(self, *, key: str, data: bytes, mime_type: str) -> StoredAsset: ...

    def get(self, storage_uri: str, *, expected_sha256: str | None = None) -> bytes: ...

    def stream(self, storage_uri: str, *, chunk_size: int = _CHUNK_SIZE) -> Iterator[bytes]: ...


class _PutHelpers(ABC):
    """Shared typed writes, expressed via the backend's `put_bytes`."""

    @abstractmethod
    def put_bytes(self, *, key: str, data: bytes, mime_type: str) -> StoredAsset: ...

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
        key = _source_key(project_id=project_id, source_id=source_id, sha256=sha, ext=extension)
        return self.put_bytes(key=key, data=data, mime_type=mime_type)

    def put_normalized_markdown(
        self,
        *,
        project_id: UUID,
        source_id: UUID,
        version_no: int,
        markdown: str,
    ) -> str:
        key = _normalized_key(project_id=project_id, source_id=source_id, version_no=version_no)
        stored = self.put_bytes(
            key=key,
            data=markdown.encode("utf-8"),
            mime_type="text/markdown; charset=utf-8",
        )
        return stored.storage_uri


class FsAssetStore(_PutHelpers):
    """Local-filesystem backend (the default).

    Writes are atomic (tmp file + `os.replace`) so a concurrent worker never
    sees a torn asset. Reads resolve keys strictly inside the root — a
    `storage_uri` that escapes it (the value comes from the DB) raises.
    """

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root).resolve()
        self._tmp = self._root / ".tmp"
        self._tmp.mkdir(parents=True, exist_ok=True)

    def put_bytes(self, *, key: str, data: bytes, mime_type: str) -> StoredAsset:
        sha = hashlib.sha256(data).hexdigest()
        dest = self._resolve(key)
        tmp = self._tmp / uuid4().hex
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
            os.replace(tmp, dest)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            msg = f"fs write failed for {key}: {exc}"
            raise AssetStoreError(msg) from exc
        return StoredAsset(
            storage_uri=f"fs://{key}",
            sha256=sha,
            size_bytes=len(data),
            mime_type=mime_type,
        )

    def get(self, storage_uri: str, *, expected_sha256: str | None = None) -> bytes:
        path = self._resolve(self._key_of(storage_uri))
        try:
            data = path.read_bytes()
        except OSError as exc:
            msg = f"fs read failed for {storage_uri}: {exc}"
            raise AssetStoreError(msg) from exc
        _verify_sha(data, expected_sha256, storage_uri)
        return data

    def stream(self, storage_uri: str, *, chunk_size: int = _CHUNK_SIZE) -> Iterator[bytes]:
        # Open eagerly (not inside the generator) so a missing file raises
        # here — before the caller commits a 200 + headers to the wire.
        path = self._resolve(self._key_of(storage_uri))
        try:
            handle = path.open("rb")
        except OSError as exc:
            msg = f"fs read failed for {storage_uri}: {exc}"
            raise AssetStoreError(msg) from exc

        def _chunks() -> Iterator[bytes]:
            with handle:
                while chunk := handle.read(chunk_size):
                    yield chunk

        return _chunks()

    @staticmethod
    def _key_of(storage_uri: str) -> str:
        if not storage_uri.startswith("fs://"):
            msg = f"not a fs:// uri (backend mismatch?): {storage_uri}"
            raise AssetStoreError(msg)
        key = storage_uri[5:]
        if not key:
            msg = f"invalid fs:// uri: {storage_uri}"
            raise AssetStoreError(msg)
        return key

    def _resolve(self, key: str) -> Path:
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root) or path == self._root:
            msg = f"asset key escapes the store root: {key}"
            raise AssetStoreError(msg)
        return path


class S3AssetStore(_PutHelpers):
    """S3-compatible backend (opt-in via ALEPH_ASSET_BACKEND=s3)."""

    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = False,
    ) -> None:
        from minio import Minio

        ep = endpoint.replace("http://", "").replace("https://", "")
        self.__client = Minio(
            endpoint=ep,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )
        self.__bucket = bucket
        if not self.__client.bucket_exists(bucket):
            self.__client.make_bucket(bucket)

    def put_bytes(self, *, key: str, data: bytes, mime_type: str) -> StoredAsset:
        sha = hashlib.sha256(data).hexdigest()
        self.__client.put_object(
            self.__bucket,
            key,
            data=BytesIO(data),
            length=len(data),
            content_type=mime_type,
        )
        return StoredAsset(
            storage_uri=f"s3://{self.__bucket}/{key}",
            sha256=sha,
            size_bytes=len(data),
            mime_type=mime_type,
        )

    def get(self, storage_uri: str, *, expected_sha256: str | None = None) -> bytes:
        resp = self._open(storage_uri)
        try:
            data = resp.read()
        finally:
            resp.close()
            resp.release_conn()
        _verify_sha(data, expected_sha256, storage_uri)
        return data

    def stream(self, storage_uri: str, *, chunk_size: int = _CHUNK_SIZE) -> Iterator[bytes]:
        # Eager fetch for the same reason as FsAssetStore.stream: a missing
        # object must raise before the caller starts a streaming response.
        resp = self._open(storage_uri)

        def _chunks() -> Iterator[bytes]:
            try:
                yield from resp.stream(chunk_size)
            finally:
                resp.close()
                resp.release_conn()

        return _chunks()

    def _open(self, storage_uri: str):  # urllib3 response type is minio-internal
        from minio.error import S3Error

        bucket, key = self._parse(storage_uri)
        try:
            return self.__client.get_object(bucket, key)
        except S3Error as exc:
            # Normalize to the store's own error so callers (the streaming
            # route's 404 mapping) treat both backends identically.
            msg = f"s3 read failed for {storage_uri}: {exc.code}"
            raise AssetStoreError(msg) from exc

    @staticmethod
    def _parse(storage_uri: str) -> tuple[str, str]:
        if not storage_uri.startswith("s3://"):
            msg = f"not an s3:// uri (backend mismatch?): {storage_uri}"
            raise AssetStoreError(msg)
        rest = storage_uri[5:]
        bucket, _, key = rest.partition("/")
        if not bucket or not key:
            msg = f"invalid s3:// uri: {storage_uri}"
            raise AssetStoreError(msg)
        return bucket, key


def create_asset_store(
    *,
    backend: Literal["fs", "s3"] = "fs",
    root: Path | str | None = None,
    endpoint: str | None = None,
    access_key: str | None = None,
    secret_key: str | None = None,
    bucket: str | None = None,
    secure: bool = False,
) -> AssetStore:
    """Build the configured backend; fail fast on incomplete s3 config."""
    if backend == "fs":
        return FsAssetStore(root or "data/assets")
    if endpoint and access_key and secret_key and bucket:
        return S3AssetStore(
            endpoint=endpoint,
            access_key=access_key,
            secret_key=secret_key,
            bucket=bucket,
            secure=secure,
        )
    required = {
        "ALEPH_S3_ENDPOINT": endpoint,
        "ALEPH_S3_ACCESS_KEY": access_key,
        "ALEPH_S3_SECRET_KEY": secret_key,
        "ALEPH_S3_BUCKET": bucket,
    }
    missing = [name for name, value in required.items() if not value]
    msg = f"s3 asset backend requires {', '.join(missing)}"
    raise AssetStoreError(msg)
