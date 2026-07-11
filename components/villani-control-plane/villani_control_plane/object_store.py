from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from .config import Settings


@dataclass(frozen=True, slots=True)
class UploadInstruction:
    method: str
    url: str
    headers: dict[str, str]
    expires_at: str


class ObjectStore(Protocol):
    def exists(self, key: str) -> bool: ...
    def put(self, key: str, stream: BinaryIO, expected_size: int) -> None: ...
    def verify(self, key: str, expected_size: int, expected_sha256: str) -> bool: ...
    def open(self, key: str) -> BinaryIO: ...
    def delete(self, key: str) -> None: ...
    def presign_upload(
        self, key: str, size: int, digest: str, expires_seconds: int
    ) -> UploadInstruction | None: ...
    def presign_download(self, key: str, expires_seconds: int) -> str | None: ...


class FilesystemObjectStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root not in path.parents:
            raise ValueError("object key escapes object-store root")
        return path

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def put(self, key: str, stream: BinaryIO, expected_size: int) -> None:
        destination = self._path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(dir=destination.parent, prefix=".upload-")
        temporary = Path(temporary_name)
        total = 0
        try:
            with os.fdopen(descriptor, "wb") as handle:
                while chunk := stream.read(1024 * 1024):
                    total += len(chunk)
                    if total > expected_size:
                        raise ValueError("artifact exceeds declared size")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if total != expected_size:
                raise ValueError("artifact size does not match descriptor")
            if destination.exists():
                temporary.unlink(missing_ok=True)
            else:
                os.replace(temporary, destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def verify(self, key: str, expected_size: int, expected_sha256: str) -> bool:
        path = self._path(key)
        if not path.is_file() or path.stat().st_size != expected_size:
            return False
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest() == expected_sha256

    def open(self, key: str) -> BinaryIO:
        return self._path(key).open("rb")

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def presign_upload(self, key: str, size: int, digest: str, expires_seconds: int):
        return None

    def presign_download(self, key: str, expires_seconds: int) -> str | None:
        return None


class S3ObjectStore:
    def __init__(self, bucket: str, *, endpoint_url: str | None, region: str | None) -> None:
        try:
            import boto3
        except ImportError as error:
            raise RuntimeError("S3 object storage requires villani-control-plane[s3]") from error
        self.bucket = bucket
        self.client = boto3.client("s3", endpoint_url=endpoint_url, region_name=region)

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return False
            raise

    def put(self, key: str, stream: BinaryIO, expected_size: int) -> None:
        raise RuntimeError("S3 uploads must use the presigned instruction")

    def verify(self, key: str, expected_size: int, expected_sha256: str) -> bool:
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        if int(response.get("ContentLength", -1)) != expected_size:
            return False
        digest = hashlib.sha256()
        for chunk in response["Body"].iter_chunks(chunk_size=1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest() == expected_sha256

    def open(self, key: str) -> BinaryIO:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"]

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def presign_upload(self, key: str, size: int, digest: str, expires_seconds: int):
        import base64
        from datetime import datetime, timedelta, timezone

        checksum = base64.b64encode(bytes.fromhex(digest)).decode("ascii")
        url = self.client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "ContentLength": size,
                "ChecksumSHA256": checksum,
                "IfNoneMatch": "*",
            },
            ExpiresIn=expires_seconds,
        )
        expires = datetime.now(timezone.utc) + timedelta(seconds=expires_seconds)
        return UploadInstruction(
            "PUT",
            url,
            {
                "Content-Length": str(size),
                "If-None-Match": "*",
                "x-amz-checksum-sha256": checksum,
            },
            expires.isoformat(),
        )

    def presign_download(self, key: str, expires_seconds: int) -> str | None:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_seconds,
        )


def create_object_store(settings: Settings) -> ObjectStore:
    if settings.object_store_backend == "filesystem":
        return FilesystemObjectStore(settings.object_store_path)
    if settings.object_store_backend == "s3":
        if not settings.s3_bucket:
            raise ValueError("S3 object storage requires s3_bucket")
        return S3ObjectStore(
            settings.s3_bucket,
            endpoint_url=settings.s3_endpoint_url,
            region=settings.s3_region,
        )
    raise ValueError(f"unknown object-store backend {settings.object_store_backend!r}")
