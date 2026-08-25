"""Local content-addressed byte artifact store (pure standard library)."""

import hashlib
import os
import re
import tempfile
from pathlib import Path


class ArtifactDigestError(ValueError):
    """Raised when a digest does not match the exact lowercase sha256 grammar."""


class ArtifactNotFoundError(FileNotFoundError):
    """Raised when an expected artifact file does not exist."""


class ArtifactIntegrityError(ValueError):
    """Raised when stored bytes do not match the requested digest."""


_SHA256_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ArtifactStore:
    """Synchronous content-addressed byte store rooted at a local directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def put_bytes(self, data: bytes) -> str:
        if type(data) is not bytes:
            raise TypeError("put_bytes requires exactly bytes")
        hex_digest = hashlib.sha256(data).hexdigest()
        digest = f"sha256:{hex_digest}"
        target = self._artifact_path(digest)
        if target.exists():
            if target.read_bytes() != data:
                raise ArtifactIntegrityError(
                    f"existing artifact does not match {digest}"
                )
            return digest
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{hex_digest}.tmp-", dir=target.parent
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, target)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                tmp_path.unlink()
            except OSError:
                pass
            raise
        return digest

    def get_bytes(self, digest: str) -> bytes:
        self._validate_digest(digest)
        path = self._artifact_path(digest)
        try:
            data = path.read_bytes()
        except FileNotFoundError as exc:
            raise ArtifactNotFoundError(digest) from exc
        if hashlib.sha256(data).hexdigest() != digest[7:]:
            raise ArtifactIntegrityError(
                f"artifact content does not match {digest}"
            )
        return data

    def exists(self, digest: str) -> bool:
        self._validate_digest(digest)
        return self._artifact_path(digest).exists()

    def verify(self, digest: str) -> bool:
        self._validate_digest(digest)
        path = self._artifact_path(digest)
        if not path.exists():
            return False
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest[7:]:
            raise ArtifactIntegrityError(
                f"artifact content does not match {digest}"
            )
        return True

    def _validate_digest(self, digest: str) -> None:
        if not isinstance(digest, str) or not _SHA256_DIGEST_RE.fullmatch(digest):
            raise ArtifactDigestError(
                "digest must be sha256:<64 lowercase hexadecimal characters>"
            )

    def _artifact_path(self, digest: str) -> Path:
        hex_digest = digest[7:]
        return self.root / "sha256" / hex_digest[:2] / hex_digest[2:]
