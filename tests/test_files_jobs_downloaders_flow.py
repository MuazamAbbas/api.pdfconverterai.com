"""Full HTTP round trip + ownership + error-envelope conformance for the
files -> jobs -> downloaders lifecycle (Handbook Part C.5/C.9), mirroring
`tests/test_files_jobs_web_tools_flow.py` for the `downloaders` module's
`POST /downloaders/upload` -> `POST /downloaders/youtube` -> poll
`GET /jobs/{id}` flow, plus the module's Tier 1 (no job queue)
`GET /downloaders/test` endpoint.

Against the real routers, real Mongo, and real Redis (enqueue only - the
worker task function is invoked directly to process the job, since no
separate `arq` worker process runs during the test suite; see
`tests/test_worker_retry.py`'s module docstring for the same tradeoff). The
real `yt_dlp.YoutubeDL`/network download is never exercised - it's
monkeypatched at `yt_dlp.YoutubeDL` itself (the same import site
`app.services.downloaders.processors.DownloadersYoutubeProcessor.execute`
uses), exactly like `tests/test_worker_retry_text_web_tools.py` monkeypatches
`fetch_webpage_text` at its definition site.
"""
import os

import pytest

import app.worker as worker

# See tests/test_worker_retry.py's module docstring/comment for why this is
# pinned to the session-scoped loop (Motor's shared `app.core.database.db`
# client).
pytestmark = pytest.mark.asyncio(loop_scope="session")


class _FakeYoutubeDL:
    """Stands in for `yt_dlp.YoutubeDL` - writes a small fake "downloaded"
    file into the processor's own per-job temp dir (derived from the real
    `outtmpl` the processor builds), so `verify()`'s
    os.path.exists/getsize(...) > 0 check and the subsequent
    `save_output_file()` registration both see a real file on disk, without
    ever touching the network or a real YouTube URL."""

    def __init__(self, opts):
        self._opts = opts
        self._output_path = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, url, download=True):
        output_dir = os.path.dirname(self._opts["outtmpl"])
        output_path = os.path.join(output_dir, "fakeid123.mp4")
        with open(output_path, "wb") as f:
            f.write(b"fake mp4 bytes, not a real video download")
        self._output_path = output_path
        return {"id": "fakeid123", "ext": "mp4", "title": "A Fake YouTube Video"}

    def prepare_filename(self, info):
        return self._output_path


async def test_downloaders_test_endpoint_returns_ok(client):
    resp = await client.get("/v1/downloaders/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["message"] == "Downloaders router is working"


async def test_upload_youtube_poll_round_trip_completes_with_fake_ytdlp(
    client, api_key, monkeypatch
):
    upload_resp = await client.post(
        "/v1/downloaders/upload",
        json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert upload_resp.status_code == 200
    upload_body = upload_resp.json()
    assert upload_body["success"] is True
    file_id = upload_body["data"]["file_id"]

    youtube_resp = await client.post(
        "/v1/downloaders/youtube",
        json={"file_id": file_id},
        headers={"X-API-Key": api_key["key"]},
    )
    assert youtube_resp.status_code == 200
    youtube_body = youtube_resp.json()
    assert youtube_body["success"] is True
    assert youtube_body["data"]["status"] == "queued"
    job_id = youtube_body["data"]["job_id"]

    monkeypatch.setattr("yt_dlp.YoutubeDL", _FakeYoutubeDL)

    # Simulate the arq worker picking the enqueued job up and processing it.
    await worker.downloaders_youtube({"job_try": 1}, job_id)

    poll_resp = await client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": api_key["key"]})
    assert poll_resp.status_code == 200
    poll_body = poll_resp.json()
    assert poll_body["success"] is True
    assert poll_body["data"]["status"] == "completed"
    output_file_id = poll_body["data"]["result"]["outputFileId"]
    assert output_file_id

    # The output file was actually registered as a downloadable `files`
    # document with the fake bytes `_FakeYoutubeDL.extract_info` wrote.
    download_resp = await client.get(
        f"/v1/files/{output_file_id}/download", headers={"X-API-Key": api_key["key"]}
    )
    assert download_resp.status_code == 200
    assert download_resp.content == b"fake mp4 bytes, not a real video download"


async def test_downloaders_upload_rejects_malformed_url(client, api_key):
    resp = await client.post(
        "/v1/downloaders/upload",
        json={"url": "not-a-valid-url"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "URL_INVALID"


async def test_youtube_rejects_nonexistent_file_id(client, api_key):
    resp = await client.post(
        "/v1/downloaders/youtube",
        json={"file_id": "0123456789ab0123456789ab"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "FILE_NOT_FOUND"


async def test_youtube_download_denied_for_file_owned_by_different_key(
    client, api_key, other_api_key
):
    upload_resp = await client.post(
        "/v1/downloaders/upload",
        json={"url": "https://www.youtube.com/watch?v=owned-by-a"},
        headers={"X-API-Key": api_key["key"]},
    )
    file_id = upload_resp.json()["data"]["file_id"]

    resp = await client.post(
        "/v1/downloaders/youtube",
        json={"file_id": file_id},
        headers={"X-API-Key": other_api_key["key"]},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "FILE_FORBIDDEN"


async def test_job_poll_denied_for_non_owner(client, api_key, other_api_key):
    upload_resp = await client.post(
        "/v1/downloaders/upload",
        json={"url": "https://www.youtube.com/watch?v=owned-by-a-poll"},
        headers={"X-API-Key": api_key["key"]},
    )
    file_id = upload_resp.json()["data"]["file_id"]
    youtube_resp = await client.post(
        "/v1/downloaders/youtube", json={"file_id": file_id}, headers={"X-API-Key": api_key["key"]}
    )
    job_id = youtube_resp.json()["data"]["job_id"]

    resp = await client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": other_api_key["key"]})
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "JOB_FORBIDDEN"
