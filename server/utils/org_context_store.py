"""Per-org memory storage on Cloudflare R2 (#1036).

One markdown document per organization, at ``mcp-org-fs/{org_id}/org/context.md``
in the bucket named by ``R2_ORG_FS_BUCKET``. The version a write replaces is kept
at ``context.md.prev``, written AFTER the new document rather than before it;
``write_context`` explains why that ordering is the safe one.

Two properties are deliberate and load-bearing:

* **The org id is validated here, again**, even though every caller derives it
  from ``get_authenticated_credentials()``. A key built from an unvalidated id
  is a path traversal into another org's memory; ``^[a-f0-9]{32}$`` makes that
  unrepresentable whatever a future caller does.
* **Unconfigured is a first-class state, not an error.** ``is_configured()``
  gates every operation, and the tools answer a role-based refusal that names
  NO environment variables -- an env var name in a user-facing error is
  operator documentation leaking to customers (and to their AI transcripts).

boto3's S3 client speaks R2's S3-compatible API directly; the credentials are
a bucket-scoped R2 API token (object read/write on ONLY the two org-fs
buckets), never an account key.
"""

import logging
import os
import re
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_ORG_ID_RE = re.compile(r"^[a-f0-9]{32}$")

_KEY_TEMPLATE = "mcp-org-fs/{org_id}/org/context.md"

# Lazily built, per-process. boto3 clients are thread-safe for use; creation
# is guarded so concurrent first calls do not build two.
_client = None
_client_lock = threading.Lock()


class OrgContextStoreError(Exception):
    """The store could not complete an operation it was configured for."""


def is_configured() -> bool:
    """True when every variable the client needs is present and non-empty."""
    return all(
        (os.getenv(name) or "").strip()
        for name in (
            "R2_ENDPOINT_URL",
            "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY",
            "R2_ORG_FS_BUCKET",
        )
    )


def _bucket() -> str:
    return os.environ["R2_ORG_FS_BUCKET"]


def _get_client():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                import boto3

                _client = boto3.client(
                    "s3",
                    endpoint_url=os.environ["R2_ENDPOINT_URL"],
                    aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
                    aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
                    region_name="auto",
                )
    return _client


def _reset_client_for_tests() -> None:
    """Tests swap credentials and fakes; production never calls this."""
    global _client
    with _client_lock:
        _client = None


def _client_or_error():
    """``_get_client()`` with construction failures turned into our own error.

    ``is_configured()`` only checks that the four variables are non-empty, so a
    malformed endpoint (no scheme, say) passes it and botocore then raises
    ValueError while BUILDING the client, before any call is made. That happens
    outside the request try-blocks below, so it escaped as a raw exception,
    which FastMCP masks into a generic "error calling tool" the model cannot
    act on. Every caller would hit it on every request until an operator fixed
    the environment.
    """
    try:
        return _get_client()
    except OrgContextStoreError:
        raise
    except Exception as exc:
        logger.warning("org context client construction failed: %s", type(exc).__name__)
        raise OrgContextStoreError(
            f"the memory store is misconfigured ({type(exc).__name__})"
        )


def _key_for(org_id: str) -> str:
    if not isinstance(org_id, str) or not _ORG_ID_RE.fullmatch(org_id):
        raise OrgContextStoreError(
            "org id is not a 32-char lowercase hex string; refusing to build "
            "a storage key from it"
        )
    return _KEY_TEMPLATE.format(org_id=org_id)


def _is_missing_key_error(exc) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    return code in {"NoSuchKey", "404", "NotFound"}


def read_context(org_id: str) -> Optional[str]:
    """The org's context document, or None when none has ever been written.

    Raises OrgContextStoreError for anything that is not a clean miss, so a
    network failure can never be mistaken for an empty memory.
    """
    key = _key_for(org_id)
    client = _client_or_error()
    try:
        response = client.get_object(Bucket=_bucket(), Key=key)
        # The .read() MUST be inside this try. botocore returns headers from
        # get_object and streams the body lazily, so a connection broken
        # mid-download raises HERE, not above. Outside the try it escaped as a
        # raw botocore exception, which FastMCP masks into a generic "error
        # calling tool" the model cannot act on. The write path below always
        # had it inside; this one did not, which is exactly the kind of
        # asymmetry that survives review.
        body = response["Body"].read()
    except Exception as exc:  # botocore's ClientError, or transport failures
        if _is_missing_key_error(exc):
            return None
        logger.warning("org context read failed for key %s: %s", key, type(exc).__name__)
        raise OrgContextStoreError(
            f"the memory store could not be read ({type(exc).__name__})"
        )
    return body.decode("utf-8", errors="replace")


def write_context(org_id: str, content: str) -> None:
    """Full-rewrite the org's context document, preserving the prior version.

    R2 has no rename and no transaction, so this is ordered to make the two
    possible partial outcomes both harmless. The current document is read
    first, the new one is written, and only then is the old one saved as
    ``.prev``.

    Writing ``.prev`` FIRST, by copying current onto it, looks safer and is
    not: when the put then fails, the current document is intact exactly as
    the error says, but ``.prev`` has been replaced by a copy of it, so the
    restore point is silently gone. Ordering it this way means a failed put
    leaves both objects untouched, and a failed ``.prev`` write leaves a
    ``.prev`` that is one version stale, which is the mild failure.
    """
    key = _key_for(org_id)
    client = _client_or_error()
    try:
        previous = client.get_object(Bucket=_bucket(), Key=key)["Body"].read()
    except Exception as exc:
        if not _is_missing_key_error(exc):
            logger.warning(
                "org context prev-read failed for key %s: %s", key, type(exc).__name__
            )
            raise OrgContextStoreError(
                f"the memory store could not preserve the previous version "
                f"({type(exc).__name__}); nothing was overwritten"
            )
        previous = None  # First-ever write: nothing to preserve.
    try:
        client.put_object(
            Bucket=_bucket(),
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8",
        )
    except Exception as exc:
        logger.warning("org context write failed for key %s: %s", key, type(exc).__name__)
        raise OrgContextStoreError(
            f"the memory store could not be written ({type(exc).__name__})"
        )
    if previous is None:
        return
    try:
        client.put_object(
            Bucket=_bucket(),
            Key=key + ".prev",
            Body=previous,
            ContentType="text/markdown; charset=utf-8",
        )
    except Exception as exc:
        # The new document is already stored, which is what the caller asked
        # for, so this is not an error to raise. It leaves a .prev one version
        # stale, and a restore point that is one version old beats failing a
        # save that succeeded.
        logger.warning(
            "org context prev-write failed for key %s: %s", key, type(exc).__name__
        )
