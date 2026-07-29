"""Full HTTP round trip + ownership + error-envelope conformance for the
files -> jobs -> text lifecycle (Handbook Part C.5/C.9), mirroring
`tests/test_files_jobs_pdf_flow.py`/`tests/test_files_jobs_image_flow.py`
for the `text` module's `POST /text/upload` -> `POST /text/paraphrase` /
`POST /text/summarize` -> poll `GET /jobs/{id}` flow, plus the module's
Tier 1 (no job queue) endpoints (`GET /text/test`, `POST /text/word_count`).

Against the real routers, real Mongo, and real Redis (enqueue only - the
worker task function is invoked directly to process the job, since no
separate `arq` worker process runs during the test suite; see
`tests/test_worker_retry.py`'s module docstring for the same tradeoff).
Success round trips pass a fake `paraphrase_pipeline`/`summarize_pipeline`
via the worker `ctx` dict, exactly like `tests/test_worker_retry_text_web_tools.py`,
since the real t5-small models are never loaded in this environment.
"""
import pytest

import app.worker as worker

# See tests/test_worker_retry.py's module docstring/comment for why this is
# pinned to the session-scoped loop (Motor's shared `app.core.database.db`
# client).
pytestmark = pytest.mark.asyncio(loop_scope="session")

_LONG_ENOUGH_TEXT = (
    "This is a sufficiently long piece of source text so that the "
    "text_summarize job's own >=50 character validation rule passes cleanly."
)
assert len(_LONG_ENOUGH_TEXT) >= 50


async def test_text_test_endpoint_returns_ok(client, api_key):
    resp = await client.get("/v1/text/test", headers={"X-API-Key": api_key["key"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["message"] == "Text Tools router is working"


async def test_upload_paraphrase_poll_round_trip_completes_with_fake_pipeline(client, api_key):
    upload_resp = await client.post(
        "/v1/text/upload",
        json={"text": "Some text to paraphrase."},
        headers={"X-API-Key": api_key["key"]},
    )
    assert upload_resp.status_code == 200
    upload_body = upload_resp.json()
    assert upload_body["success"] is True
    file_id = upload_body["data"]["file_id"]

    paraphrase_resp = await client.post(
        "/v1/text/paraphrase",
        json={"file_id": file_id},
        headers={"X-API-Key": api_key["key"]},
    )
    assert paraphrase_resp.status_code == 200
    paraphrase_body = paraphrase_resp.json()
    assert paraphrase_body["success"] is True
    assert paraphrase_body["data"]["status"] == "queued"
    job_id = paraphrase_body["data"]["job_id"]

    def _fake_pipeline(input_text, **kwargs):
        return [{"generated_text": "A fake but plausible paraphrase."}]

    # Simulate the arq worker picking the enqueued job up and processing it.
    await worker.text_paraphrase({"job_try": 1, "paraphrase_pipeline": _fake_pipeline}, job_id)

    poll_resp = await client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": api_key["key"]})
    assert poll_resp.status_code == 200
    poll_body = poll_resp.json()
    assert poll_body["success"] is True
    assert poll_body["data"]["status"] == "completed"
    assert poll_body["data"]["result"]["paraphrased"] == "A fake but plausible paraphrase."


async def test_upload_summarize_poll_round_trip_completes_with_fake_pipeline(client, api_key):
    upload_resp = await client.post(
        "/v1/text/upload",
        json={"text": _LONG_ENOUGH_TEXT},
        headers={"X-API-Key": api_key["key"]},
    )
    assert upload_resp.status_code == 200
    file_id = upload_resp.json()["data"]["file_id"]

    summarize_resp = await client.post(
        "/v1/text/summarize",
        json={"file_id": file_id},
        headers={"X-API-Key": api_key["key"]},
    )
    assert summarize_resp.status_code == 200
    summarize_body = summarize_resp.json()
    assert summarize_body["success"] is True
    assert summarize_body["data"]["status"] == "queued"
    job_id = summarize_body["data"]["job_id"]

    def _fake_pipeline(text, max_length=50, min_length=10, do_sample=False, num_beams=4, length_penalty=1.0):
        return [{"summary_text": "A fake but plausible summary."}]

    await worker.text_summarize({"job_try": 1, "summarize_pipeline": _fake_pipeline}, job_id)

    poll_resp = await client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": api_key["key"]})
    assert poll_resp.status_code == 200
    poll_body = poll_resp.json()
    assert poll_body["success"] is True
    assert poll_body["data"]["status"] == "completed"
    assert poll_body["data"]["result"]["summary"] == "A fake but plausible summary."


async def test_text_upload_rejects_empty_text(client, api_key):
    resp = await client.post(
        "/v1/text/upload",
        json={"text": "   "},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "TEXT_INPUT_EMPTY"


async def test_paraphrase_rejects_nonexistent_file_id(client, api_key):
    resp = await client.post(
        "/v1/text/paraphrase",
        json={"file_id": "0123456789ab0123456789ab"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "FILE_NOT_FOUND"


async def test_summarize_too_short_text_job_ends_failed_not_retried_over_http(client, api_key):
    """`TextSummarizeProcessor.validate()` (`app/services/text/processors.py`)
    rejects source text under 50 characters as a permanent failure - the
    upload endpoint itself only rejects empty/whitespace-only text, so a
    too-short-but-non-empty string uploads fine and only fails once the
    summarize job actually runs, mirroring
    `test_corrupt_pdf_job_ends_failed_not_retried_over_http` in
    `tests/test_files_jobs_pdf_flow.py`.
    """
    upload_resp = await client.post(
        "/v1/text/upload",
        json={"text": "too short"},
        headers={"X-API-Key": api_key["key"]},
    )
    file_id = upload_resp.json()["data"]["file_id"]

    summarize_resp = await client.post(
        "/v1/text/summarize",
        json={"file_id": file_id},
        headers={"X-API-Key": api_key["key"]},
    )
    job_id = summarize_resp.json()["data"]["job_id"]

    await worker.text_summarize({"job_try": 1}, job_id)

    poll_resp = await client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": api_key["key"]})
    body = poll_resp.json()
    assert poll_resp.status_code == 200  # polling itself succeeds; the *job* failed
    assert body["data"]["status"] == "failed"
    assert body["data"]["error"]
    assert "Traceback" not in body["data"]["error"]


async def test_job_poll_denied_for_non_owner(client, api_key, other_api_key):
    upload_resp = await client.post(
        "/v1/text/upload",
        json={"text": "Some text owned by key A."},
        headers={"X-API-Key": api_key["key"]},
    )
    file_id = upload_resp.json()["data"]["file_id"]
    paraphrase_resp = await client.post(
        "/v1/text/paraphrase", json={"file_id": file_id}, headers={"X-API-Key": api_key["key"]}
    )
    job_id = paraphrase_resp.json()["data"]["job_id"]

    resp = await client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": other_api_key["key"]})
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "JOB_FORBIDDEN"


async def test_word_count_endpoint_returns_count(client, api_key):
    """Tier 1 (no job queue) endpoint sanity check - `text`'s other
    Tier 1 endpoints (`char_count`/`sentence_count`/`paragraph_count`/
    `grammar`) share the same `TextRequest` request model and response
    envelope, so this stands in for that whole group."""
    resp = await client.post(
        "/v1/text/word_count",
        json={"text": "four little words"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["word_count"] == 3


async def test_word_count_endpoint_rejects_empty_text(client, api_key):
    """`TextRequest.text` has `min_length=1` (`app/models/text.py`), so an
    empty string never reaches `word_count()` - it's rejected by FastAPI's
    request validation with the same Handbook Part C.5 envelope every other
    validation error gets (see
    `test_missing_api_key_header_rejected` in
    `tests/test_files_jobs_pdf_flow.py` for the same handler)."""
    resp = await client.post(
        "/v1/text/word_count",
        json={"text": ""},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
