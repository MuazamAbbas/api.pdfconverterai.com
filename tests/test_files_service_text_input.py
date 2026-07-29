"""Unit tests for `app.services.files.service.save_text_input` (Handbook
Part C.3/C.9) - no existing test file exercises `app/services/files/service.py`
functions directly today (`tests/test_files_jobs_pdf_flow.py`/
`tests/test_files_jobs_image_flow.py` only exercise `save_uploaded_file`
indirectly, through the `/files/upload`/`/pdf/upload`/`/image/upload` HTTP
routes), so this is a new, narrowly-scoped module for the new
`save_text_input` helper the `text`/`web_tools` Tier 1 upload endpoints added
(`POST /text/upload`, `POST /web_tools/upload`).

Uses real local Mongo (per this project's testing convention - `save_text_input`
inserts a `files` document) via the `api_key` fixture from `tests/conftest.py`,
whose own teardown (`_cleanup_api_key`) already deletes every file (Mongo doc
+ on-disk bytes) owned by that key, so no separate cleanup is needed here.
"""
import pytest
from bson import ObjectId

from app.core.config import settings
from app.services.files.service import UploadValidationError, get_file_by_id, save_text_input

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_save_text_input_happy_path_writes_file_and_mongo_record(api_key):
    owner_id = ObjectId(api_key["id"])
    text = "Some plain text input to paraphrase or summarize later."

    file_doc = await save_text_input(text, owner_id, "text_input.txt")

    assert file_doc.mimeType == "text/plain"
    assert file_doc.originalFilename == "text_input.txt"
    assert file_doc.sizeBytes == len(text.encode("utf-8"))
    assert str(file_doc.ownerApiKeyId) == str(owner_id)

    with open(file_doc.storagePath, "r", encoding="utf-8") as f:
        assert f.read() == text

    # Round-trips through get_file_by_id the same way a Tier 2 job would.
    fetched = await get_file_by_id(str(file_doc.id))
    assert fetched is not None
    assert fetched.storagePath == file_doc.storagePath


async def test_save_text_input_sanitizes_original_filename(api_key):
    owner_id = ObjectId(api_key["id"])

    file_doc = await save_text_input("some url or text", owner_id, "../../etc/passwd")

    assert ".." not in file_doc.originalFilename
    assert "/" not in file_doc.originalFilename and "\\" not in file_doc.originalFilename


@pytest.mark.parametrize("empty_text", ["", "   ", "\n\t  \n"])
async def test_save_text_input_rejects_empty_or_whitespace_only_text(api_key, empty_text):
    owner_id = ObjectId(api_key["id"])

    with pytest.raises(UploadValidationError) as exc_info:
        await save_text_input(empty_text, owner_id, "text_input.txt")

    assert exc_info.value.error_code == "TEXT_INPUT_EMPTY"
    assert exc_info.value.status_code == 400


async def test_save_text_input_rejects_oversized_text(api_key, monkeypatch):
    monkeypatch.setattr(settings, "max_upload_size_mb", 1)  # 1 MiB cap for this test
    owner_id = ObjectId(api_key["id"])
    too_big_text = "x" * (1024 * 1024 + 1)

    with pytest.raises(UploadValidationError) as exc_info:
        await save_text_input(too_big_text, owner_id, "text_input.txt")

    assert exc_info.value.error_code == "FILE_TOO_LARGE"
    assert exc_info.value.status_code == 413


async def test_get_file_by_id_returns_none_for_malformed_id():
    assert await get_file_by_id("not-a-valid-object-id") is None


async def test_get_file_by_id_returns_none_for_well_formed_but_nonexistent_id():
    assert await get_file_by_id("0123456789ab0123456789ab") is None
