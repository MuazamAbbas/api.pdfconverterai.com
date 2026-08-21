import html
import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def generate_meta_tags(title: str, description: str, keywords: Optional[str]) -> dict:
    """
    Build a basic HTML meta-tag snippet (`<title>`, meta description, meta
    keywords) from user-supplied fields.

    Args:
        title (str): Page title.
        description (str): Page meta description.
        keywords (Optional[str]): Comma-separated keyword list.
    Returns:
        dict: original field values plus the generated `html_snippet`.
    Raises:
        ValueError: If title or description is empty/whitespace-only.
    """
    if not title or not title.strip():
        logger.error("Title is empty")
        raise ValueError("Title cannot be empty")
    if not description or not description.strip():
        logger.error("Description is empty")
        raise ValueError("Description cannot be empty")

    # SECURITY: html.escape() is mandatory on every field interpolated
    # below. title/description/keywords are raw, untrusted user input and
    # this function's output is an HTML string. Without escaping, a title
    # like `</title><script>...</script>` would be reflected verbatim into
    # the generated snippet - a stored/reflected XSS vector for any
    # downstream consumer (frontend or otherwise) that renders this HTML
    # (Handbook Part C.10 - safe by default).
    safe_title = html.escape(title)
    safe_description = html.escape(description)
    safe_keywords = html.escape(keywords) if keywords else ""

    snippet_lines = [
        f"<title>{safe_title}</title>",
        f'<meta name="description" content="{safe_description}">',
    ]
    if safe_keywords:
        snippet_lines.append(f'<meta name="keywords" content="{safe_keywords}">')

    html_snippet = "\n".join(snippet_lines)

    result = {
        "title": title,
        "description": description,
        "keywords": keywords,
        "html_snippet": html_snippet,
    }
    logger.debug("Meta tags generated (snippet length=%d)", len(html_snippet))
    return result
