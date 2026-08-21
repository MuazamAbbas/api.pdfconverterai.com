import html
import logging
from typing import Optional

logger = logging.getLogger(__name__)


async def generate_social_media_tags(
    title: str, description: str, image_url: str, url: Optional[str]
) -> dict:
    """
    Build an Open Graph + Twitter Card HTML meta-tag snippet from
    user-supplied fields.

    Args:
        title (str): Page/content title.
        description (str): Page/content description.
        image_url (str): Preview image URL.
        url (Optional[str]): Canonical URL for the content.
    Returns:
        dict: original field values plus the generated `html_snippet`.
    Raises:
        ValueError: If title, description, or image_url is empty/
            whitespace-only.
    """
    if not title or not title.strip():
        logger.error("Title is empty")
        raise ValueError("Title cannot be empty")
    if not description or not description.strip():
        logger.error("Description is empty")
        raise ValueError("Description cannot be empty")
    if not image_url or not image_url.strip():
        logger.error("Image URL is empty")
        raise ValueError("Image URL cannot be empty")

    # SECURITY: html.escape() is mandatory on every field interpolated
    # below, including image_url and url. All four land inside an HTML
    # attribute (`content="..."`), so an unescaped value containing `">`
    # could break out of the attribute and inject markup/script - a
    # stored/reflected XSS vector for any downstream consumer that renders
    # this HTML (Handbook Part C.10 - safe by default). This applies
    # equally to image_url/url even though they "look like" URLs - they are
    # still untrusted user input at this layer.
    safe_title = html.escape(title)
    safe_description = html.escape(description)
    safe_image_url = html.escape(image_url)
    safe_url = html.escape(url) if url else ""

    tags = [
        f'<meta property="og:title" content="{safe_title}">',
        f'<meta property="og:description" content="{safe_description}">',
        f'<meta property="og:image" content="{safe_image_url}">',
    ]
    if safe_url:
        tags.append(f'<meta property="og:url" content="{safe_url}">')
    tags.extend(
        [
            '<meta name="twitter:card" content="summary_large_image">',
            f'<meta name="twitter:title" content="{safe_title}">',
            f'<meta name="twitter:description" content="{safe_description}">',
            f'<meta name="twitter:image" content="{safe_image_url}">',
        ]
    )

    html_snippet = "\n".join(tags)

    result = {
        "title": title,
        "description": description,
        "image_url": image_url,
        "url": url,
        "html_snippet": html_snippet,
    }
    logger.debug("Social media tags generated (snippet length=%d)", len(html_snippet))
    return result
