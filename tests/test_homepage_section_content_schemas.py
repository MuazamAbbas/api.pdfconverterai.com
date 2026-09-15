"""Pure Pydantic-level unit tests for the `homepage_section.py` per-type
content models added in task #43 (`SliderContent`/`BlogNewsContent`).

Deliberately synchronous and DB/HTTP-free - unlike
`tests/test_admin_homepage_sections.py` (which exercises these same shapes
indirectly through the CRUD routes and carries a module-wide
`pytest.mark.asyncio` mark), these tests only need the schema layer, so
they live in their own module rather than picking up an async harness they
don't use.
"""
import pytest
from pydantic import ValidationError

from app.schemas.homepage_section import BlogNewsContent, SliderContent


def _slide(n: int) -> dict:
    return {"image_url": f"https://cdn.example.com/slide-{n}.jpg"}


# --- SliderContent ----------------------------------------------------


def test_slider_content_accepts_single_slide():
    content = SliderContent(slides=[_slide(1)])
    assert len(content.slides) == 1


def test_slider_content_accepts_eight_slides():
    content = SliderContent(slides=[_slide(i) for i in range(8)])
    assert len(content.slides) == 8


def test_slider_content_rejects_zero_slides():
    with pytest.raises(ValidationError):
        SliderContent(slides=[])


def test_slider_content_rejects_nine_slides():
    with pytest.raises(ValidationError):
        SliderContent(slides=[_slide(i) for i in range(9)])


def test_slider_content_rejects_non_http_image_url():
    with pytest.raises(ValidationError):
        SliderContent(slides=[{"image_url": "javascript:alert(1)"}])


def test_slider_content_rejects_extra_field():
    with pytest.raises(ValidationError):
        SliderContent(slides=[_slide(1)], unexpected="nope")


def test_slider_content_accepts_optional_heading_and_link():
    content = SliderContent(
        slides=[
            {
                "image_url": "https://cdn.example.com/slide-1.jpg",
                "heading": "Big sale",
                "link": {"label": "Shop now", "href": "/shop"},
            }
        ]
    )
    assert content.slides[0].heading == "Big sale"
    assert content.slides[0].link.href == "/shop"


# --- BlogNewsContent ----------------------------------------------------


def test_blog_news_content_accepts_with_category():
    content = BlogNewsContent(heading="Latest from the blog", count=3, category="pdf-tips")
    assert content.category == "pdf-tips"


def test_blog_news_content_accepts_without_category():
    content = BlogNewsContent(heading="Latest from the blog", count=3)
    assert content.category is None


def test_blog_news_content_accepts_count_boundaries():
    assert BlogNewsContent(heading="Latest", count=1).count == 1
    assert BlogNewsContent(heading="Latest", count=6).count == 6


def test_blog_news_content_rejects_count_zero():
    with pytest.raises(ValidationError):
        BlogNewsContent(heading="Latest", count=0)


def test_blog_news_content_rejects_count_seven():
    with pytest.raises(ValidationError):
        BlogNewsContent(heading="Latest", count=7)


def test_blog_news_content_rejects_missing_heading():
    with pytest.raises(ValidationError):
        BlogNewsContent(count=3)


def test_blog_news_content_rejects_extra_field():
    with pytest.raises(ValidationError):
        BlogNewsContent(heading="Latest", count=3, unexpected="nope")
