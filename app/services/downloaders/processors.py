"""Concrete Processor for the `downloaders` module's single Tier 2 job,
`downloaders_youtube` (Handbook Part C.4, ADR-003). Mirrors
`app/services/web_tools/processors.py`'s `WebToolsSummarizeProcessor` -
implements the shared Validate/Prepare/Execute/Verify/Cleanup interface
from `app/services/jobs/processor.py`; `app/worker.py`'s
`downloaders_youtube` task function is the thin ARQ-facing wrapper that
calls `.run()` and owns the Job's Pending/Queued/Processing/Completed/
Failed transitions.

`downloaders` depends on `jobs` here (imports its Processor base), matching
the Handbook Part C.3 one-direction dependency chain (... file -> job ->
downloaders) - `jobs` never imports anything from `downloaders`.

The input `files` document holds a URL as text, written by `save_text_input`
the same way `web_tools_summarize`'s input is (`app/routers/downloaders.py`'s
`upload_downloaders`) - `_read_url_input` is reused from
`app.services.web_tools.processors` rather than duplicated here.

`yt_dlp` is imported inside `execute()` rather than at module scope, so
importing this module (or running some other job type) never pulls in
yt_dlp unless a `downloaders_youtube` job actually runs - see
`app/worker.py`'s module docstring for why that matters, and matching
`PdfToWordProcessor`'s `convert_pdf_to_word`/`PdfSummarizeProcessor`'s
`summarize_pdf_service` lazy-import precedent in
`app/services/pdf/processors.py`.
"""
import logging
import os

from app.core.config import settings
from app.core.storage import STORAGE_PATH
from app.services.jobs.processor import (
    PermanentProcessingError,
    Processor,
    TransientProcessingError,
)
from app.services.web_tools.processors import _read_url_input

logger = logging.getLogger(__name__)

# `format: "best"` (a single pre-merged stream, see `execute()`) can land on
# any of these containers depending on what YouTube offers for a given
# video - there's no way to know the extension until after `extract_info()`
# runs. Known limitation: anything not in this map falls back to
# "application/octet-stream" rather than a guessed video/* type.
_EXT_MIME_TYPES: dict[str, str] = {
    "mp4": "video/mp4",
    "webm": "video/webm",
    "mkv": "video/x-matroska",
    "m4v": "video/x-m4v",
    "3gp": "video/3gpp",
    "flv": "video/x-flv",
}

# Fallback only. yt_dlp's `DownloadError` wraps the underlying
# `ExtractorError` it caught, reachable via `DownloadError.exc_info[1]`, and
# `ExtractorError` already computes a purpose-built `.expected` boolean
# (true for "normal"/permanent failures like unavailable/private/unsupported
# videos, false for genuine bugs) - see `execute()`, which checks that first.
# This substring list only kicks in if a `DownloadError` doesn't carry that
# nested `ExtractorError` (e.g. some other failure path inside yt_dlp), since
# relying on message text alone is fragile against yt_dlp wording changes
# across releases.
_PERMANENT_ERROR_MARKERS: tuple[str, ...] = (
    "unsupported url",
    "no video formats found",
    "video unavailable",
    "private video",
    "this video is unavailable",
    "has been removed",
    "account associated with this video has been terminated",
    "sign in to confirm your age",
    "age-restricted",
    "video is not available",
    "does not exist",
    "copyright",
    "unable to extract",
    "requested format is not available",
)


class DownloadersYoutubeProcessor(Processor):
    """job.type == "downloaders_youtube" """

    async def validate(self, job, file_doc):
        url = _read_url_input(file_doc)
        if not url.startswith(("http://", "https://")):
            raise PermanentProcessingError("URL must start with http:// or https://")

    async def prepare(self, job, file_doc):
        # Write flat into the shared `STORAGE_PATH`, same as every other
        # processor (`PdfToWordProcessor`, etc.) - no per-job subdirectory.
        # `app/core/storage.py::cleanup_expired_files` only ever deletes
        # individual files (`os.remove`, both on its tracked-expiry and
        # orphan-sweep paths), never recurses into or removes directories,
        # so a nested `mkdtemp` dir would silently leak on disk forever
        # (verified against `_delete_path`/`cleanup_expired_files` directly -
        # this isn't a hypothetical). The `job.id` prefix keeps this job's
        # output filename collision-free against every other concurrent
        # job/processor sharing the same flat directory.
        return {"url": _read_url_input(file_doc), "output_prefix": f"ytdlp-{job.id}-"}

    async def execute(self, job, file_doc, prepared, ctx=None):
        import yt_dlp

        ydl_opts = {
            "format": "best",
            "outtmpl": os.path.join(STORAGE_PATH, prepared["output_prefix"] + "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            # This job registers exactly one output `files` document
            # (`save_output_file` in `app/worker.py`'s `build_result`) - a
            # playlist URL producing multiple files would break that
            # one-job-one-output-file assumption, so restrict to a single
            # video even if the URL happens to resolve to a playlist entry.
            "noplaylist": True,
            # Force the `web` player client explicitly (Handbook/
            # SPRINT_STATUS.md 2026-07-30 finding): yt_dlp's default client
            # priority (android_vr, web_safari, ...) still hit YouTube's
            # "Sign in to confirm you're not a bot" IP-reputation check on
            # this VPS even with a PO Token provider configured below -
            # `web` was the one client verified working end-to-end in that
            # investigation, alongside the PO Token + JS runtime settings.
            "extractor_args": {
                "youtube": {"player_client": ["web"]},
                # Only needed if `bgutil_pot_provider_url` isn't the
                # plugin's own default (127.0.0.1:4416, auto-detected) -
                # set explicitly anyway so a future non-default port/host
                # doesn't silently fall back to no PO Token provider.
                # Real key is hyphenated ("youtubepot-bgutilhttp") - yt_dlp's
                # extractor_args IE-key lookup does NOT normalize hyphens to
                # underscores when passed via the Python API (only the CLI
                # string parser does, and only for values within an IE's
                # args, not the IE key itself) - confirmed against
                # `BgUtilHTTPPTP.PROVIDER_KEY` and yt_dlp's PO-token
                # director (`youtubepot-{provider.PROVIDER_KEY.lower()}`).
                # An earlier underscored version of this key was a silent
                # no-op, caught by code review before merge.
                "youtubepot-bgutilhttp": {"base_url": [settings.bgutil_pot_provider_url]},
            },
        }
        # Only pass a cookiefile if it actually exists on disk - yt_dlp
        # raises at YoutubeDL init time if a configured cookiefile path is
        # missing, and local/dev environments won't necessarily have
        # `settings.youtube_cookie_file` present.
        if os.path.exists(settings.youtube_cookie_file):
            ydl_opts["cookiefile"] = settings.youtube_cookie_file
        else:
            logger.debug(
                "youtube_cookie_file %s not found - proceeding without cookies",
                settings.youtube_cookie_file,
            )
        # Only enable the `node` JS runtime (needed by the `yt-dlp-ejs`
        # package to solve YouTube's signature/n-parameter challenges for
        # the `web` client) if the configured Node.js binary actually
        # exists - local/dev environments won't have the dedicated /opt
        # install this VPS uses. Without this, `web` client extraction
        # still runs but silently loses some formats (see the "Signature
        # solving failed" warning found during investigation) rather than
        # hard-failing, so this is a best-effort enhancement, not a
        # hard requirement the way cookiefile/PO-Token-provider are.
        if os.path.exists(settings.youtube_js_runtime_node_path):
            ydl_opts["js_runtimes"] = {"node": {"path": settings.youtube_js_runtime_node_path}}
        else:
            logger.debug(
                "youtube_js_runtime_node_path %s not found - proceeding without a JS runtime",
                settings.youtube_js_runtime_node_path,
            )

        # yt_dlp is a synchronous/blocking library, called directly here
        # rather than wrapped in a thread executor - matching this
        # codebase's existing precedent for blocking per-tool libraries
        # called from inside an `async def execute()`/service function
        # (`app/services/pdf/pdf_to_word.py`'s `pdf2docx.Converter.convert`,
        # `app/services/image/ocr.py`'s `pytesseract.image_to_string`, and
        # this same `yt_dlp.YoutubeDL` call already made this way in
        # `app/services/video/youtube_metadata.py`), not a new concurrency
        # pattern invented for this job.
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(prepared["url"], download=True)
                output_path = ydl.prepare_filename(info)
        except yt_dlp.utils.DownloadError as e:
            expected = None
            exc_info = getattr(e, "exc_info", None)
            if exc_info and len(exc_info) > 1:
                expected = getattr(exc_info[1], "expected", None)
            if expected is None:
                # No usable `ExtractorError.expected` signal on this
                # exception - fall back to message-substring matching.
                message = str(e).lower()
                expected = any(marker in message for marker in _PERMANENT_ERROR_MARKERS)
            if expected:
                raise PermanentProcessingError(f"Could not download this video: {e}") from e
            raise TransientProcessingError("Temporary error downloading the video") from e
        except Exception as e:
            # Anything else (e.g. a filesystem hiccup writing the output) is
            # treated as an unexpected transient fault, same fallback
            # `WebToolsSummarizeProcessor.execute()` uses around its
            # summarization step.
            raise TransientProcessingError("Temporary error downloading the video") from e

        # Record what execute() actually produced back onto the shared
        # `prepared` dict (not just the returned result) - `cleanup()` only
        # receives `prepared`, not this method's return value (see
        # `app/services/jobs/processor.py::Processor.run()`), so this is how
        # cleanup() finds out whether a real output file exists.
        prepared["output_path"] = output_path

        ext = info.get("ext", "")
        return {
            "output_path": output_path,
            "title": info.get("title") or file_doc.originalFilename,
            "ext": ext,
            "mime_type": _EXT_MIME_TYPES.get(ext, "application/octet-stream"),
        }

    async def verify(self, job, file_doc, result):
        output_path = result.get("output_path")
        if not output_path or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise PermanentProcessingError("YouTube download produced no output file")

    async def cleanup(self, job, file_doc, prepared):
        output_path = prepared.get("output_path")
        produced_valid_output = (
            output_path and os.path.exists(output_path) and os.path.getsize(output_path) > 0
        )
        if produced_valid_output:
            # IMPORTANT: `Processor.run()` calls `cleanup()` in a `finally`
            # block *before* returning to `app/worker.py::_run_job`, which
            # only afterwards calls `build_result` -> `save_output_file()`
            # (which registers `local_path` directly as the new `files`
            # document's `storagePath`, it does not copy the bytes
            # elsewhere first). So cleanup() runs strictly before the
            # output file is handed off/registered - deleting it here would
            # make `save_output_file` register a path that no longer
            # exists. Leave the file in place; the existing files/jobs TTL
            # retention sweep (`app/core/storage.py::cleanup_expired_files`)
            # removes it once the output file's `files` document expires -
            # the same flat-file-in-STORAGE_PATH lifecycle `pdf_to_word`'s
            # output already relies on, and (unlike a nested per-job
            # directory) something that sweep actually reaches.
            return
        # Nothing usable was produced (validation/download/verify failed
        # before a real output file existed) - nothing downstream will ever
        # reference this path, so it's safe to remove it right away instead
        # of waiting on the TTL sweep.
        if output_path and os.path.exists(output_path):
            try:
                os.remove(output_path)
            except OSError as e:
                logger.warning("Failed to remove incomplete download %s: %s", output_path, e)
