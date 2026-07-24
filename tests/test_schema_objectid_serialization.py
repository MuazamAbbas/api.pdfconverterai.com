"""Tests for `app.schemas.common.PyObjectId`'s serialization behavior.

Confirms the class's own docstring promise: values stay real `bson.ObjectId`
instances through plain "python" mode `model_dump()` (what
`app/services/files/service.py`/`app/services/jobs/service.py` call right
before `insert_one(...)`), so `files.ownerApiKeyId`/`jobs.fileId` are written
to Mongo as proper BSON ObjectIds - the same BSON type as the `_id` they
reference, for equality queries/joins to work. JSON-mode serialization (API
responses) still stringifies them.

Previously `plain_serializer_function_ser_schema(str)` had no `when_used=`
argument, which defaults to `"always"` instead of `"json"` - so even
python-mode `model_dump()` stringified these fields, contradicting the
docstring. Fixed by pinning `when_used="json"` on that serializer.
"""
from bson import ObjectId

from app.schemas.file import FileCreate


def test_owner_api_key_id_stays_a_real_objectid_via_python_mode_model_dump():
    owner_id = ObjectId()
    file_create = FileCreate(
        storagePath="/tmp/whatever.pdf",
        checksum="a" * 64,
        originalFilename="whatever.pdf",
        sizeBytes=10,
        mimeType="application/pdf",
        ownerApiKeyId=owner_id,
    )

    # Python-mode model_dump() (used right before Mongo inserts) keeps a
    # real ObjectId, matching the BSON type of the `_id` it references.
    dumped = file_create.model_dump(by_alias=True)
    assert isinstance(dumped["ownerApiKeyId"], ObjectId)
    assert dumped["ownerApiKeyId"] == owner_id


def test_owner_api_key_id_stringifies_in_json_mode():
    owner_id = ObjectId()
    file_create = FileCreate(
        storagePath="/tmp/whatever.pdf",
        checksum="a" * 64,
        originalFilename="whatever.pdf",
        sizeBytes=10,
        mimeType="application/pdf",
        ownerApiKeyId=owner_id,
    )

    # JSON-mode serialization (used for API responses) still stringifies it.
    json_dumped = file_create.model_dump(mode="json", by_alias=True)
    assert isinstance(json_dumped["ownerApiKeyId"], str)
    assert json_dumped["ownerApiKeyId"] == str(owner_id)
