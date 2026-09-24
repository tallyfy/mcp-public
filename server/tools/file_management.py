"""
File Management Tools
Read and write Tallyfy assets (files uploaded to form fields, kickoff fields
and task attachments).

WHY THIS MODULE EXISTS (tallyfy/mcp#493, #494, and the #465 blocker behind both)
-------------------------------------------------------------------------------
A file uploaded to a Tallyfy File Upload field was reachable from the Angular web
UI and from nowhere else an MCP client could get to. The tools that used to try
imported host-side modules (``database.connection``, ``files.extractors``,
``files.storage``) that do not exist in this process, which is the runtime error
#465 records. These two tools close that gap through the Tallyfy REST API, with
no host dependency:

* ``get_asset_content`` -- ``GET /organizations/{org}/assets/{id}`` for metadata,
  then ``GET /organizations/{org}/file/{id}/dl`` for the bytes.
* ``upload_asset``      -- ``POST /organizations/{org}/assets`` as multipart.

THE BYTES COME FROM ``/file/{id}/dl``, NOT ``/file/{id}`` (#1348)
-----------------------------------------------------------------
#1340 read ``/file/{id}``, and that route answers 404 for a file on a task in a
running process. Read off api-v2 master: ``AssetsController::getObject`` REBUILDS
the storage key from the run's own ``checklist_id`` column, which is the template
VERSION. The upload stored the object under the template id the uploader sent,
which is the template's timeline id (``RunTransformer`` exposes
``blueprint_timeline`` as ``checklist_id``), and run files have lived under the
timeline id since ``ASSETS_PATH_CHANGED_DATE`` in 2019. ``downloadFile``, behind
``/file/{id}/dl``, reads the ``file_path`` the upload wrote and only rebuilds a
key for an old asset that has none. It is also the route both Tallyfy web
clients read every file through (``files.service.ts``), so it is the one that
works for kickoff files and task files alike.

Two consequences of that route, both of them api-v2's own behaviour and the same
as downloading the file in the browser:

* It answers ``application/octet-stream`` for every file, so ``mime_type`` is
  inferred from the filename's extension. See ``_mime_type_for``.
* It writes a "File downloaded" entry to the activity feed. The tool stays
  ``readOnlyHint=True`` because it changes nothing a user owns.

THE SDK CANNOT CARRY EITHER CALL, so these go over httpx directly
-----------------------------------------------------------------
``BaseSDK._make_request`` always tries ``response.json()`` and returns a parsed
dict, and its only body modes are JSON and ``application/x-www-form-urlencoded``.
A download needs raw bytes and an upload needs ``multipart/form-data``, so neither
fits. ``tools/search.py`` already reaches for ``httpx`` inside a tool, so this is
an established shape here rather than a new one.

They still raise ``TallyfyError`` rather than ``ToolError`` on an upstream status,
which is deliberate: that is what plugs them into ``handle_tallyfy_errors`` like
every other tool, so a 401 here reaches ``flag_downstream_auth_failure`` and gets
the same re-authentication challenge, and a 404 is demoted to a warning by
``EXPECTED_UPSTREAM_STATUSES`` instead of paging Sentry. A bare ``ToolError``
would skip all of that. ``ToolError`` is kept for what this tool itself refuses,
before anything is sent.

THE DOWNLOAD IS STREAMED, AND THE CAP IS NOT COSMETIC
-----------------------------------------------------
Tallyfy accepts uploads up to 100MB. ``httpx.get`` on a 100MB asset buffers 100MB
inside a shared production server process, so the read goes through
``httpx.stream`` with a hard byte cap: an asset over the cap is never pulled into
memory at all, and the tool answers with metadata plus a stated reason instead.
Partial content is never returned as ``content_base64``, because half a base64
string decodes to a corrupt file and nothing downstream can tell that from a
whole one.

``compact_result`` IS DELIBERATELY NOT CALLED ON THE PAYLOAD HERE, and this is
the one tidy-up to resist. It truncates to fit ``MAX_RESULT_BYTES``; applied to a
base64 blob that silently produces content that no longer decodes. For a dict
with no ``data`` list it is a no-op that logs a warning anyway, so calling it
would buy nothing and cost correctness the day somebody "improves" it.
"""

import base64
import binascii
import logging
import mimetypes
import re
from typing import Annotated, Any, Dict, Optional

import httpx
from pydantic import Field

from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.types import ToolAnnotations
from tallyfy import TallyfyError

from metrics import track_tool_execution
from utils.auth_context import get_authenticated_credentials, TALLYFY_API_BASE_URL
from utils.fastmcp_errors import _sanitize_api_error, handle_tallyfy_errors
from utils.sdk_serializer import MAX_RESULT_BYTES

logger = logging.getLogger(__name__)


# The transport cap, in RAW bytes, before base64. #493 asks for "a reasonable
# limit (e.g. 10MB raw / ~13MB base64)" and this is it. A module constant rather
# than an environment variable on purpose: a second deployment-dependent value
# is a second thing no reader can determine from this repository, and this one
# has no operator reason to differ per deployment.
MAX_ASSET_BYTES = 10 * 1024 * 1024

# Reading an asset can take a while over a slow link at the cap above, so the
# transfer calls get their own timeout rather than the 30s the metadata call
# uses (which mirrors tools/api_fallback.py).
METADATA_TIMEOUT_SECONDS = 30.0
TRANSFER_TIMEOUT_SECONDS = 60.0

# The subject_type values api-v2 actually accepts on an upload (#1348). These are
# api-v2's MODEL names. #1340 sent "Template" and "Process", copied from the
# Swagger annotation on AssetsController::store (enum={"Template","Process"}),
# which is wrong. Read off api-v2 master rather than off that annotation:
#
# * Models/Concerns/MorphSubject::setSubjectTypeAttribute resolves a short name
#   only when Tallyfy\API\V1\Models\<name> is a real class. Checklist and Run
#   are; Template and Process are not, so they were stored as literal strings.
# * Asset::$validation_rules names the vocabulary, in:Run,Checklist,Organization.
#   Nothing enforces it on this route (UploadAssetRequest says only `required`),
#   which is why a wrong value was accepted rather than refused.
# * The failure comes AFTER the damage. store() writes the row and the S3 object
#   in saveFile, then dispatches the activity-feed event, whose lookup in
#   ActivityFeed::TARGETED_ENTITY_RELATED_COLUMN is keyed by class name. A
#   literal "Template" throws `Undefined array key`, answered as HTTP 500, with
#   the file already stored. That is why upload_asset's error path says a
#   server error may have left a copy behind.
# * Both Tallyfy web clients send the subject from render-field.component.ts:
#   isPrerun ? 'Checklist' : task.is_oneoff_task ? 'Task' : 'Run'.
#
# 'Task' is the third value, for a form field on a STANDALONE (one-off) task,
# and it is a different SHAPE, not just a different word (#1352). Read off
# api-v2 master and both web clients:
#
# * The web clients send uploaded_from=<field id>, subject_type='Task',
#   subject_id=<the task's own id>, and NEITHER step_id NOR checklist_id. Both
#   add those two keys only when the task is not one-off (client-v2
#   render-field.component.ts saveFile, legacy render.field.component.js).
# * MorphSubject resolves 'Task' to the real Task model class, so it is stored
#   as a model, unlike the literal 'Template' of #1348. UploadAssetRequest says
#   only `required`, so the Asset::$validation_rules list, which omits Task, is
#   not applied on this route (nothing calls validateInput there).
# * saveFile keys the object as checklists/{subject_id}/ when no checklist_id
#   is sent, and records that key in file_path, which /file/{id}/dl reads
#   first. A checklist_id would move it to checklists/{checklist_id}/runs/
#   {task id}/, a path nothing else writes, which is why this tool refuses one.
# * The activity-feed step that runs after the store looks the subject up in
#   ActivityFeed::TARGETED_ENTITY_RELATED_COLUMN, and Task::class is in it. It
#   has to be: AssetsService::wrapFileUpload rewrites every process-task upload
#   to Task::class before that same step.
#
# That last point is also why a bare 'Task' is REFUSED rather than mapped. The
# API answers subject.type 'Task' for a process-task upload too, so a model
# that has just read one will reach for 'Task' with a run id, and api-v2 would
# store the file against a task that does not exist. The standalone shape is
# named StandaloneTask, the word this server uses for these tasks everywhere
# else (create_standalone_task, update_standalone_task).
_WIRE_KICKOFF = "Checklist"
_WIRE_PROCESS = "Run"
_WIRE_STANDALONE_TASK = "Task"

# What `subject_type` a caller may say, mapped to the wire value above. The
# caller vocabulary is Template, Process and StandaloneTask, which is what the
# rest of this server calls these things. Checklist and Run are accepted too,
# for the reason _FOLDER_TYPE_ALIASES in tools/folder_management.py gives:
# api-v2 answers with them in the `subject.type` it returns, so a model that
# has just read one asset will reach for that word when writing the next.
# Keys carry no spaces, hyphens or underscores, because _subject_type_key
# strips those before the lookup ("standalone task", "one-off task").
_SUBJECT_TYPE_ALIASES = {
    "template": _WIRE_KICKOFF,
    "templates": _WIRE_KICKOFF,
    "checklist": _WIRE_KICKOFF,
    "blueprint": _WIRE_KICKOFF,
    "process": _WIRE_PROCESS,
    "processes": _WIRE_PROCESS,
    "run": _WIRE_PROCESS,
    "standalonetask": _WIRE_STANDALONE_TASK,
    "standalonetasks": _WIRE_STANDALONE_TASK,
    "oneofftask": _WIRE_STANDALONE_TASK,
    "oneofftasks": _WIRE_STANDALONE_TASK,
}

# Words that name a task without saying which kind. Refused with a message
# naming both task shapes, never guessed. See the comment above.
_AMBIGUOUS_TASK_WORDS = frozenset({"task", "tasks"})

_KICKOFF_FIELD_MARKER = "ko_field"

# httpx errors raised before any of the request left this process, so the file
# cannot have reached Tallyfy. Every other httpx.RequestError can fire after the
# body was sent (a ReadTimeout is the usual one), and then Tallyfy may already
# have stored it.
_NOT_SENT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

_RETRY_WARNING = (
    "Do not upload it again automatically, because each retry can store another "
    "copy. Tell the user the upload may have partly gone through."
)

# A PRIVATE table rather than the module-level mimetypes.guess_type. The module
# functions read whatever mime.types files the host happens to carry, so the same
# filename could get a different answer in the container, in CI and on a laptop.
# A MimeTypes() instance starts from Python's built-in table only. That table
# has no Office formats in Python 3.13, and a .docx or .xlsx is among the most
# common things a Tallyfy form collects, so those three are added by hand.
_MIME_TYPES = mimetypes.MimeTypes()
for _ext, _type in (
    (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    (".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
):
    _MIME_TYPES.add_type(_type, _ext)

# What the download route says about every file, which tells a caller nothing.
_GENERIC_CONTENT_TYPES = frozenset({"application/octet-stream", "binary/octet-stream"})


# ---------------------------------------------------------------------------
# Parameter types
# ---------------------------------------------------------------------------
# Declared here rather than in utils/fastmcp_types.py because every one of them
# is used by exactly one tool in this file. A SHARED alias carrying its own
# `description=` OVERRIDES each function's Args entry on every parameter that
# uses it, which is #1251; a local, single-use annotation cannot do that.

AssetId = Annotated[str, Field(
    min_length=32,
    max_length=32,
    pattern="^[a-f0-9]{32}$",
    description=(
        "Asset ID of the uploaded file, as a 32-character hex string. Read it "
        "from get_task, where a File Upload field's taskdata value carries the "
        "asset, or from a task comment attachment, or from the response of a "
        "previous upload_asset call."
    ),
    examples=["a1b2c3d4e5f6789012345678901234ef"],
)]

AssetContentBase64 = Annotated[str, Field(
    min_length=1,
    description=(
        "The file itself, base64-encoded. Standard base64, with or without "
        "padding; whitespace and newlines are ignored. Decoded size must be at "
        "or under 10MB."
    ),
    examples=["SGVsbG8gVGFsbHlmeQ=="],
)]

AssetFilename = Annotated[str, Field(
    min_length=1,
    max_length=255,
    description=(
        "Original filename including its extension. Tallyfy shows this to "
        "people and uses the extension to decide how to preview the file, so "
        "send report.pdf rather than report."
    ),
    examples=["report.pdf", "expenses-2026-q1.csv"],
)]

UploadedFrom = Annotated[str, Field(
    min_length=1,
    max_length=64,
    description=(
        "Which field the file belongs to. Pass the literal ko_field for a "
        "kickoff form field on a template, or the 32-character hex field ID for "
        "a form field on a task, either a task in a running process or a "
        "standalone task."
    ),
    examples=["ko_field", "a1b2c3d4e5f6789012345678901234ef"],
)]

SubjectType = Annotated[str, Field(
    min_length=1,
    max_length=32,
    description=(
        "What the file is being attached to: Template for a kickoff form "
        "upload, Process for an upload to a task inside a running process, or "
        "StandaloneTask for an upload to a form field on a standalone "
        "(one-off) task. Checklist is accepted as a synonym of Template and Run "
        "as a synonym of Process, because that is the vocabulary the API "
        "answers with. A bare Task is refused, because it could mean either "
        "kind of task."
    ),
    examples=["Template", "Process", "StandaloneTask"],
)]

SubjectId = Annotated[str, Field(
    min_length=32,
    max_length=32,
    pattern="^[a-f0-9]{32}$",
    description=(
        "ID of the thing the file is attached to, as a 32-character hex string: "
        "the template ID when subject_type is Template, the process (run) ID "
        "when subject_type is Process, or the task's own ID when subject_type "
        "is StandaloneTask."
    ),
    examples=["c7d8e9f0a1b2c3d4e5f60718293a4b5c"],
)]

# max_length with a pattern that also admits "", and no min_length, exactly as
# OptionalProcessId does and for the same measured reason: a model that emits ""
# rather than omitting an optional field is common, and Pydantic rejects it
# BEFORE the function body runs, so no in-body guard can rescue it.
OptionalStepIdForUpload = Annotated[Optional[str], Field(
    default=None,
    max_length=32,
    pattern="^(?:[a-f0-9]{32})?$",
    description=(
        "Step ID that owns the form field, as a 32-character hex string. "
        "Required when subject_type is Process. Leave it out for a kickoff "
        "upload, which belongs to the template rather than to any step, and "
        "for StandaloneTask, because a standalone task has no step."
    ),
    examples=["b2c3d4e5f6a7890123456789012345ab"],
)]

OptionalChecklistIdForUpload = Annotated[Optional[str], Field(
    default=None,
    max_length=32,
    pattern="^(?:[a-f0-9]{32})?$",
    description=(
        "Template ID the process was launched from, as a 32-character hex "
        "string. Required when subject_type is Process. This is the TEMPLATE, "
        "not the process: never repeat subject_id here. Leave it out for "
        "StandaloneTask."
    ),
    examples=["a1b2c3d4e5f6789012345678901234ef"],
)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _api_headers(api_key: str, accept: str) -> Dict[str, str]:
    """The three headers every go.tallyfy.com/api call needs.

    ``X-Tallyfy-Client`` is not optional decoration: without it the edge answers
    some routes with its own 404 body (error code apsh546) before the request
    ever reaches Laravel, which reads exactly like a route that was never
    deployed.

    ``accept`` varies on purpose. The metadata call asks for JSON so an error
    comes back as JSON. The file call asks for anything, because the response is
    binary and a JSON-only Accept invites a 406 on a route whose whole job is to
    stream a file.
    """
    return {
        "Authorization": f"Bearer {api_key}",
        "X-Tallyfy-Client": "APIClient",
        "Accept": accept,
    }


def _upstream_message(response: httpx.Response) -> Any:
    """The API's own error body, parsed when it is JSON and raw text otherwise."""
    try:
        return response.json()
    except ValueError:
        return response.text


def _is_ok(status_code: int) -> bool:
    """2xx only.

    ``>= 400`` is the obvious test and it is wrong HERE, on the file route. The
    API streams the bytes itself rather than handing back a presigned S3 URL, so
    a 3xx means that contract changed. ``follow_redirects`` is off (httpx's
    default), so a 3xx carries an EMPTY body, and treating it as success would
    publish a zero-byte file as if it were the real one. Refusing is the loud
    half, and a 3xx is not in ``EXPECTED_UPSTREAM_STATUSES`` so it reaches
    Sentry, which is right for a contract change nobody announced.

    If the API ever does start redirecting, turning ``follow_redirects`` on is
    safe as far as the credential goes: httpx's ``_redirect_headers`` drops the
    ``Authorization`` header on any cross-origin redirect that is not a plain
    http-to-https upgrade, so the Bearer token would not reach S3. Read off
    httpx 0.28.1 rather than assumed. It is still a deliberate change with its
    own test, not a default to drift into.
    """
    return 200 <= status_code < 300


def _raise_upstream(response: httpx.Response, what: str) -> None:
    """Turn a non-2xx into the TallyfyError the shared decorator understands.

    Deliberately NOT a ToolError. See the module docstring: raising the SDK's
    own error type is what routes a 401 into the downstream auth challenge and
    keeps a 404 out of Sentry.
    """
    body = _upstream_message(response)
    message = what
    if isinstance(body, dict) and body.get("message"):
        message = f"{what}: {body['message']}"
    elif isinstance(body, str) and body.strip():
        message = f"{what}: {body.strip()[:500]}"
    raise TallyfyError(message, status_code=response.status_code, response_data=body)


def _raise_upload_failure(response: httpx.Response, filename: str) -> None:
    """Turn a failed upload into a TallyfyError that says what may have happened.

    A 4xx is refused before anything is stored (a FormRequest, an auth check or
    a size limit), so it goes through ``_raise_upstream`` unchanged.

    A 5xx is different, and #1348 is the measured case: api-v2 stores the row
    and the S3 object in ``saveFile`` and can fail on the step after that, so a
    server error does not mean nothing was saved. Telling the model nothing was
    stored is what makes it retry, and every retry stores another copy.

    The warning has to travel in ``response_data["message"]``, not only in the
    exception's own message. ``handle_tallyfy_errors`` builds the text the model
    sees from ``response_data["message"]`` when there is one and drops the rest,
    which is why a model saw only ``Undefined array key "Template"``. The
    upstream body is kept alongside it, so the Sentry context still carries the
    API's own words. The status code is kept too, so a 5xx still logs at ERROR.
    """
    status = response.status_code
    if status < 500:
        _raise_upstream(response, f"{filename} could not be uploaded")

    body = _upstream_message(response)
    message = (
        f"Tallyfy reported a server error while handling {filename}. It may "
        "already have stored the file, because it saves an upload before its "
        f"last step. {_RETRY_WARNING}"
    )
    # The API's own words are quoted only when they are a JSON message that the
    # shared sanitizer leaves alone. JSON, because a 5xx can be an HTML error page
    # from the edge. Unchanged by the sanitizer, because it cuts the WHOLE message
    # at the first leaked internal (a SQLSTATE, a PHP path), which would leave a
    # dangling "Tallyfy said" or, placed first, cut the warning itself. The full
    # body still reaches Sentry through response_data.
    if isinstance(body, dict) and body.get("message"):
        said = str(body["message"]).strip()
        if said and _sanitize_api_error(said) == said:
            message = f"{message} Tallyfy said: {said}"
    raise TallyfyError(
        message,
        status_code=status,
        response_data={"message": message, "upstream_response": body},
    )


def _mime_type_for(declared: Optional[str], filename: Optional[str]) -> Optional[str]:
    """The file's MIME type, when the download route will not say.

    ``/file/{id}/dl`` answers ``application/octet-stream`` for every file, so a
    specific type the server declares is kept (in case that ever changes) and a
    generic one is replaced by a guess from the filename's extension. When there
    is no usable extension the declared value is returned as it was.
    """
    base = (declared or "").split(";", 1)[0].strip().lower()
    if base and base not in _GENERIC_CONTENT_TYPES:
        return declared
    if filename:
        guessed, _encoding = _MIME_TYPES.guess_file_type(filename, strict=False)
        if guessed:
            return guessed
    return declared or None


def _decode_base64(content_base64: str) -> bytes:
    """Decode strictly, and say which of the two things went wrong.

    ``validate=True`` is the point. Without it base64 silently DISCARDS every
    character outside the alphabet, so a truncated or corrupted string decodes
    to a shorter file that uploads successfully and is quietly wrong. Real
    whitespace is stripped first so a wrapped MIME-style string still works.
    """
    compact = "".join(content_base64.split())
    if not compact:
        raise ToolError(
            "content_base64 is empty. Pass the file's bytes base64-encoded."
        )
    # The parameter description promises "with or without padding", so honour it.
    # Python refuses an unpadded string with "Incorrect padding" in binascii.a2b_base64,
    # and that applies with AND without validate=True, so dropping strict mode would
    # not accept it: padding here is the only fix that keeps the promise. Right-padding
    # cannot make corrupt input decode, because validate=True still rejects any
    # character outside the alphabet. tallyfy/mcp#1340 review finding.
    compact += "=" * (-len(compact) % 4)
    try:
        return base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ToolError(
            f"content_base64 is not valid base64 ({exc}). Encode the raw file "
            "bytes with standard base64; do not send a data: URL, a file path, "
            "or the file's text content."
        ) from exc


def _subject_type_key(subject_type: str) -> str:
    """Lowercase, with spaces, hyphens and underscores removed."""
    return re.sub(r"[\s_-]+", "", str(subject_type)).lower()


def _normalize_subject_type(subject_type: str) -> str:
    key = _subject_type_key(subject_type)
    if key in _AMBIGUOUS_TASK_WORDS:
        raise ToolError(
            f"subject_type {subject_type!r} could mean either kind of task. Use "
            "'StandaloneTask' for a form field on a standalone (one-off) task, "
            "with subject_id set to that task's id. Use 'Process' for a task "
            "inside a running process, with subject_id set to the process id "
            "plus step_id and checklist_id."
        )
    resolved = _SUBJECT_TYPE_ALIASES.get(key)
    if resolved is None:
        raise ToolError(
            f"subject_type must be 'Template' (a kickoff form upload), "
            f"'Process' (an upload to a task in a running process) or "
            f"'StandaloneTask' (an upload to a form field on a standalone "
            f"task) -- got {subject_type!r}"
        )
    return resolved


def _blank_to_none(value: Optional[str]) -> Optional[str]:
    """Treat "" as omitted. The annotations above accept it on purpose."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def register_file_management_tools(mcp):
    """Register all file/asset management tools with the MCP server"""

    @mcp.tool(
        name="get_asset_content",
        description=(
            "Read a file that was uploaded to Tallyfy. Returns the file's "
            "metadata (filename, MIME type, size in bytes) and its bytes as "
            "base64 in content_base64. Use this whenever you need the contents "
            "of a file attached to a File Upload form field, a kickoff field, "
            "or a task comment. Get the asset_id from get_task, where a File "
            "Upload field's taskdata value carries it, or from a previous "
            "upload_asset response. Files over 10MB are NOT downloaded: you get "
            "the metadata plus content_omitted_reason saying why, and "
            "content_base64 is null. Partial content is never returned, so a "
            "non-null content_base64 is always the whole file. Reading a file "
            "records a download in Tallyfy's activity feed, the same as "
            "downloading it in the browser."
        ),
        tags=["file", "asset", "read-only", "attachment"],
        annotations=ToolAnnotations(
            title="Get file content from Tallyfy",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("get_asset_content")
    @handle_tallyfy_errors("get asset content")
    def get_asset_content(asset_id: AssetId) -> ToolResult:
        """
        Fetch file metadata and base64-encoded content from Tallyfy.

        Args:
            asset_id: The asset ID of the uploaded file, a 32-character hex
                string. Obtained from get_task (a File Upload field's taskdata
                value), from a task comment attachment, or from an earlier
                upload_asset response.

        Returns:
            ToolResult with id, filename, version, mime_type (inferred from the
            filename's extension, because Tallyfy's download route labels every
            file application/octet-stream), size_bytes,
            content_base64 (null when the file was too large to download),
            content_omitted_reason (null when the content is present),
            size_warning (null unless the base64 exceeds this server's
            result budget), uploaded_from, step_id, source and subject
            {id, type}.
        """
        api_key, org_id = get_authenticated_credentials()
        base = f"{TALLYFY_API_BASE_URL}/organizations/{org_id}"

        try:
            meta_response = httpx.get(
                f"{base}/assets/{asset_id}",
                headers=_api_headers(api_key, "application/json"),
                timeout=METADATA_TIMEOUT_SECONDS,
            )
        except httpx.RequestError as exc:
            raise ToolError(
                f"Could not reach Tallyfy to look up asset {asset_id} "
                f"({type(exc).__name__}). Nothing was read; try again."
            ) from exc

        if not _is_ok(meta_response.status_code):
            _raise_upstream(meta_response, f"asset {asset_id} could not be read")

        meta_body = _upstream_message(meta_response)
        if not isinstance(meta_body, dict):
            raise ToolError(
                f"Tallyfy returned a non-JSON response for asset {asset_id}, so "
                "its metadata could not be read."
            )
        meta = meta_body.get("data")
        if not isinstance(meta, dict):
            raise ToolError(
                f"Tallyfy's response for asset {asset_id} carried no data "
                "object, so the file could not be identified."
            )

        payload: Dict[str, Any] = {
            "id": meta.get("id", asset_id),
            "filename": meta.get("filename"),
            "version": meta.get("version"),
            "uploaded_from": meta.get("uploaded_from"),
            "step_id": meta.get("step_id"),
            "source": meta.get("source"),
            "subject": meta.get("subject"),
            "mime_type": None,
            "size_bytes": None,
            "content_base64": None,
            "content_omitted_reason": None,
            "size_warning": None,
        }

        try:
            # /dl, not /file/{id}: the bare route rebuilds the storage key and
            # misses every file on a task in a running process (#1348). See the
            # module docstring.
            with httpx.stream(
                "GET",
                f"{base}/file/{asset_id}/dl",
                headers=_api_headers(api_key, "*/*"),
                timeout=TRANSFER_TIMEOUT_SECONDS,
            ) as file_response:
                if not _is_ok(file_response.status_code):
                    file_response.read()
                    _raise_upstream(
                        file_response,
                        f"the stored file for asset {asset_id} could not be "
                        f"downloaded (it may have been removed from storage)",
                    )

                payload["mime_type"] = _mime_type_for(
                    file_response.headers.get("content-type"),
                    payload.get("filename"),
                )

                # The cheap path: the API declares the length, so an oversized
                # file is refused without transferring a single byte of it.
                declared = file_response.headers.get("content-length")
                declared_bytes: Optional[int] = None
                if declared is not None:
                    try:
                        declared_bytes = int(declared)
                    except ValueError:
                        declared_bytes = None

                if declared_bytes is not None and declared_bytes > MAX_ASSET_BYTES:
                    payload["size_bytes"] = declared_bytes
                    payload["content_omitted_reason"] = (
                        f"The file is {declared_bytes} bytes, over the "
                        f"{MAX_ASSET_BYTES}-byte limit this tool downloads. Its "
                        "metadata is above; open the file in Tallyfy to read it."
                    )
                    return ToolResult(content=payload, structured_content=None)

                chunks = []
                total = 0
                over_cap = False
                for chunk in file_response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_ASSET_BYTES:
                        over_cap = True
                        break
                    chunks.append(chunk)
        except httpx.RequestError as exc:
            raise ToolError(
                f"Could not download the file for asset {asset_id} "
                f"({type(exc).__name__}). Its metadata was read but its content "
                "was not; try again."
            ) from exc

        if over_cap:
            # No size_bytes: the transfer stopped early, so the only honest
            # answer is that the file is larger than the cap. Reporting the
            # bytes read so far would be a number that looks like a file size
            # and is not one.
            payload["content_omitted_reason"] = (
                f"The file is larger than the {MAX_ASSET_BYTES}-byte limit this "
                "tool downloads, and Tallyfy did not declare its exact size. "
                "Its metadata is above; open the file in Tallyfy to read it."
            )
            return ToolResult(content=payload, structured_content=None)

        raw = b"".join(chunks)
        payload["size_bytes"] = len(raw)
        payload["content_base64"] = base64.b64encode(raw).decode("ascii")

        # Say so rather than letting the caller discover it. A base64 blob is
        # about 4/3 of the file, and anything near MAX_RESULT_BYTES is large
        # enough that the client may refuse or trim the result downstream.
        if len(payload["content_base64"]) > MAX_RESULT_BYTES:
            payload["size_warning"] = (
                f"content_base64 is {len(payload['content_base64'])} characters, "
                f"over this server's {MAX_RESULT_BYTES}-byte result budget. The "
                "content is complete here, but the client may truncate it. For a "
                "large file, work from filename and mime_type instead."
            )

        return ToolResult(content=payload, structured_content=None)

    @mcp.tool(
        name="upload_asset",
        description=(
            "Upload a file to a Tallyfy form field. Give it the file as base64 "
            "in content_base64 plus a filename, and say where it goes. There "
            "are three shapes. (1) A KICKOFF field on a template: "
            "uploaded_from='ko_field', subject_type='Template', "
            "subject_id=<template id>. (2) A form field on a task in a running "
            "process: uploaded_from=<field id>, subject_type='Process', "
            "subject_id=<process id>, and BOTH step_id (the step that owns the "
            "field) and checklist_id (the template the process was launched "
            "from) are required. (3) A form field on a STANDALONE task, the "
            "one-off kind made by create_standalone_task: "
            "uploaded_from=<field id>, subject_type='StandaloneTask', "
            "subject_id=<that task's id>, and no step_id or checklist_id. A "
            "bare 'Task' is refused, because it could mean shape 2 or shape 3. "
            "Returns the new asset's metadata including its id, which you then "
            "pass in the taskdata of update_task (shape 2) or "
            "update_standalone_task (shape 3) to record the file against the "
            "field. Creates a new asset every call and replaces nothing, so "
            "calling it twice uploads the file twice. Decoded content must be "
            "at or under 10MB."
        ),
        tags=["file", "asset", "write", "attachment"],
        annotations=ToolAnnotations(
            title="Upload file to Tallyfy",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
        output_schema=None
    )
    @track_tool_execution("upload_asset")
    @handle_tallyfy_errors("upload asset")
    def upload_asset(
        content_base64: AssetContentBase64,
        filename: AssetFilename,
        uploaded_from: UploadedFrom,
        subject_type: SubjectType,
        subject_id: SubjectId,
        step_id: OptionalStepIdForUpload = None,
        checklist_id: OptionalChecklistIdForUpload = None,
    ) -> ToolResult:
        """
        Upload a file to a Tallyfy form field or kickoff field.

        Args:
            content_base64: The file's bytes, base64-encoded. At or under 10MB
                once decoded.
            filename: Original filename including its extension, for example
                report.pdf.
            uploaded_from: The literal ko_field for a kickoff form field, or the
                32-character hex field ID for a form field on a task, whether
                the task is in a running process or is a standalone task.
            subject_type: Template for a kickoff upload, Process for an upload
                to a task in a running process, or StandaloneTask for an upload
                to a form field on a standalone (one-off) task. Checklist and
                Run are accepted as synonyms of the first two. A bare Task is
                refused, because it could mean either kind of task.
            subject_id: The template ID when subject_type is Template, the
                process (run) ID when subject_type is Process, or the task's
                own ID when subject_type is StandaloneTask.
            step_id: The step that owns the form field. Required when
                subject_type is Process. Leave it out for StandaloneTask.
            checklist_id: The template the process was launched from. Required
                when subject_type is Process, never the same value as
                subject_id, and left out for StandaloneTask.

        Returns:
            ToolResult with the created asset's id, filename, version,
            uploaded_from, uploaded_at, step_id, source and subject {id, type}.
            For a process-task upload api-v2 reports the subject as the Task and
            its id, while the stored asset belongs to the process:
            get_asset_content then shows type Run and the process id. Measured
            on staging for #1348; it is api-v2's AssetsService::wrapFileUpload
            rewriting the subject in memory before the response is rendered.
            For a standalone-task upload the stored subject IS the task, so
            both the response and get_asset_content show type Task and the
            task id (read from api-v2 source for #1352: wrapFileUpload rewrites
            only a Run subject).
        """
        resolved_subject_type = _normalize_subject_type(subject_type)
        step_id = _blank_to_none(step_id)
        checklist_id = _blank_to_none(checklist_id)

        # The web clients send neither key for a standalone task, and api-v2
        # would take a checklist_id as an instruction to file the object under
        # checklists/{checklist_id}/runs/{task id}/, a path no other writer
        # uses. Refused before anything is sent, so the caller learns which
        # shape it meant rather than getting a 201 on the wrong one.
        if resolved_subject_type == _WIRE_STANDALONE_TASK:
            extra = [
                name for name, value in
                (("step_id", step_id), ("checklist_id", checklist_id))
                if value
            ]
            if extra:
                raise ToolError(
                    f"A standalone task has no step or template, so leave "
                    f"{' and '.join(extra)} out when subject_type is "
                    "StandaloneTask. If the task is inside a running process, "
                    "use subject_type 'Process' with the process id instead."
                )

        # Refused here rather than at the API, because api-v2 accepts the upload
        # and stores an asset that is attached to nothing reachable. A caller
        # then sees success and no file on the field.
        if resolved_subject_type == _WIRE_PROCESS:
            missing = [
                name for name, value in
                (("step_id", step_id), ("checklist_id", checklist_id))
                if not value
            ]
            if missing:
                raise ToolError(
                    f"Uploading to a task form field needs {' and '.join(missing)}. "
                    "step_id is the step that owns the field and checklist_id is "
                    "the template the process was launched from; get both from "
                    "get_task or get_template_steps."
                )
            if checklist_id == subject_id:
                raise ToolError(
                    "checklist_id is the TEMPLATE the process was launched from, "
                    "not the process itself, so it cannot equal subject_id. Read "
                    "the template id from get_process."
                )

        raw = _decode_base64(content_base64)
        if len(raw) > MAX_ASSET_BYTES:
            raise ToolError(
                f"{filename} is {len(raw)} bytes once decoded, over the "
                f"{MAX_ASSET_BYTES}-byte limit this tool uploads. Nothing was "
                "sent. Upload a file this size through Tallyfy in the browser."
            )

        api_key, org_id = get_authenticated_credentials()

        form: Dict[str, str] = {
            "uploaded_from": uploaded_from,
            "subject_type": resolved_subject_type,
            "subject_id": subject_id,
            "source": "local",
        }
        if step_id:
            form["step_id"] = step_id
        if checklist_id:
            form["checklist_id"] = checklist_id

        try:
            response = httpx.post(
                f"{TALLYFY_API_BASE_URL}/organizations/{org_id}/assets",
                headers=_api_headers(api_key, "application/json"),
                data=form,
                # httpx sets the multipart Content-Type and boundary itself. Do
                # not add one to the headers: a hand-written Content-Type has no
                # boundary and the API cannot parse the body.
                files={"name": (filename, raw)},
                timeout=TRANSFER_TIMEOUT_SECONDS,
            )
        except _NOT_SENT_ERRORS as exc:
            raise ToolError(
                f"Could not reach Tallyfy to upload {filename} "
                f"({type(exc).__name__}). Nothing was uploaded; try again."
            ) from exc
        except httpx.RequestError as exc:
            # Not "nothing was uploaded": a ReadTimeout, say, fires after the
            # whole body was sent, and api-v2 may have stored it by then.
            raise ToolError(
                f"The connection to Tallyfy failed while uploading {filename} "
                f"({type(exc).__name__}), after the file may already have been "
                f"sent, so Tallyfy may have stored it. {_RETRY_WARNING}"
            ) from exc

        if not _is_ok(response.status_code):
            _raise_upload_failure(response, filename)

        body = _upstream_message(response)
        if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
            raise ToolError(
                f"{filename} was accepted by Tallyfy but the response carried no "
                "asset data, so the new asset's id could not be read. Check the "
                "field in Tallyfy before uploading again."
            )

        logger.info(
            "upload_asset stored %s as asset %s on %s %s",
            filename, body["data"].get("id"), resolved_subject_type, subject_id,
        )
        return ToolResult(content=body["data"], structured_content=None)
