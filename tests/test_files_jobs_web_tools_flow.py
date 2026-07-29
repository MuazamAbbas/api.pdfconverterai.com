"""Full HTTP round trip + ownership + error-envelope conformance for the
files -> jobs -> web_tools lifecycle (Handbook Part C.5/C.9), mirroring
`tests/test_files_jobs_pdf_flow.py`/`tests/test_files_jobs_image_flow.py`
for the `web_tools` module's `POST /web_tools/upload` ->
`POST /web_tools/summarize` -> poll `GET /jobs/{id}` flow, plus the
module's Tier 1 (no job queue) endpoints (`GET /web_tools/test`,
`POST /web_tools/url_encode`).

Against the real routers, real Mongo, and real Redis (enqueue only - the
worker task function is invoked directly to process the job, since no
separate `arq` worker process runs during the test suite; see
`tests/test_worker_retry.py`'s module docstring for the same tradeoff).
The real page fetch is never exercised - `fetch_webpage_text` is
monkeypatched at its definition site, exactly like
`tests/test_worker_retry_text_web_tools.py`, and a fake `summarize_pipeline`
stands in for the real t5-small model via the worker `ctx` dict.
"""
import pytest

import app.worker as worker

# See tests/test_worker_retry.py's module docstring/comment for why this is
# pinned to the session-scoped loop (Motor's shared `app.core.database.db`
# client).
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_web_tools_test_endpoint_returns_ok(client, api_key):
    resp = await client.get("/v1/web_tools/test", headers={"X-API-Key": api_key["key"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["message"] == "Web Tools router is working"


async def test_upload_summarize_poll_round_trip_completes_with_fake_pipeline(client, api_key, monkeypatch):
    upload_resp = await client.post(
        "/v1/web_tools/upload",
        json={"url": "https://example.com/article"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert upload_resp.status_code == 200
    upload_body = upload_resp.json()
    assert upload_body["success"] is True
    file_id = upload_body["data"]["file_id"]

    summarize_resp = await client.post(
        "/v1/web_tools/summarize",
        json={"file_id": file_id},
        headers={"X-API-Key": api_key["key"]},
    )
    assert summarize_resp.status_code == 200
    summarize_body = summarize_resp.json()
    assert summarize_body["success"] is True
    assert summarize_body["data"]["status"] == "queued"
    job_id = summarize_body["data"]["job_id"]

    async def _fake_fetch(url):
        return "Fetched webpage paragraph text, long enough to summarize."

    def _fake_pipeline(text, max_length=150, min_length=30, do_sample=False):
        return [{"summary_text": "A fake but plausible webpage summary."}]

    monkeypatch.setattr("app.services.web_tools.summarize.fetch_webpage_text", _fake_fetch)

    # Simulate the arq worker picking the enqueued job up and processing it.
    await worker.web_tools_summarize({"job_try": 1, "summarize_pipeline": _fake_pipeline}, job_id)

    poll_resp = await client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": api_key["key"]})
    assert poll_resp.status_code == 200
    poll_body = poll_resp.json()
    assert poll_body["success"] is True
    assert poll_body["data"]["status"] == "completed"
    assert poll_body["data"]["result"]["summary"] == "A fake but plausible webpage summary."


async def test_web_tools_upload_rejects_malformed_url(client, api_key):
    resp = await client.post(
        "/v1/web_tools/upload",
        json={"url": "not-a-valid-url"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "URL_INVALID"


async def test_summarize_rejects_nonexistent_file_id(client, api_key):
    resp = await client.post(
        "/v1/web_tools/summarize",
        json={"file_id": "0123456789ab0123456789ab"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "FILE_NOT_FOUND"


async def test_job_poll_denied_for_non_owner(client, api_key, other_api_key):
    upload_resp = await client.post(
        "/v1/web_tools/upload",
        json={"url": "https://example.com/owned-by-a"},
        headers={"X-API-Key": api_key["key"]},
    )
    file_id = upload_resp.json()["data"]["file_id"]
    summarize_resp = await client.post(
        "/v1/web_tools/summarize", json={"file_id": file_id}, headers={"X-API-Key": api_key["key"]}
    )
    job_id = summarize_resp.json()["data"]["job_id"]

    resp = await client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": other_api_key["key"]})
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "JOB_FORBIDDEN"


async def test_url_encode_endpoint_returns_encoded_url(client, api_key):
    """Tier 1 (no job queue) endpoint sanity check - `url_encode` doesn't
    go through the Job System at all, unlike `summarize`."""
    resp = await client.post(
        "/v1/web_tools/url_encode",
        json={"url": "https://example.com/a b"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["encoded_url"] == "https%3A//example.com/a%20b"
