import logging

from app.core.config import settings


def setup_logging() -> None:
    """Configure the root logger's handlers exactly once, for the whole
    process. Must be called from `app/main.py` before any router/service
    module is imported - every other module only calls
    `logging.getLogger(__name__)` and must never call
    `logging.basicConfig()` itself, since `basicConfig()` is a no-op once a
    handler already exists on the root logger. Scattering `basicConfig()`
    calls across modules made the outcome depend on which module happened
    to import first, which is what silently stopped error.log from
    receiving writes (SPRINT_STATUS.md, 2026-07-29 finding).
    """
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(settings.downloaders_log_path),
            logging.StreamHandler(),
        ],
    )
