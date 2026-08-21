import logging
import re

logger = logging.getLogger(__name__)

# Standard Flesch Reading Ease score bands -> human-readable difficulty
# label. Checked highest-threshold-first; a score >= threshold gets that
# band's label.
_DIFFICULTY_BANDS = [
    (90.0, "Very Easy"),
    (80.0, "Easy"),
    (70.0, "Fairly Easy"),
    (60.0, "Standard"),
    (50.0, "Fairly Difficult"),
    (30.0, "Difficult"),
    (float("-inf"), "Very Difficult"),
]


def _count_syllables(word: str) -> int:
    """
    Vowel-group heuristic: count syllables as the number of contiguous
    vowel groups in the word, with a silent-trailing-"e" adjustment and a
    floor of 1 per non-empty word. This is the standard approximation used
    by most readability tools - not phonetically perfect, but accurate
    enough to feed a Flesch-style score.
    """
    cleaned = re.sub(r"[^a-z]", "", word.lower())
    if not cleaned:
        return 0
    groups = re.findall(r"[aeiouy]+", cleaned)
    count = len(groups)
    if cleaned.endswith("e") and not cleaned.endswith("le") and count > 1:
        count -= 1
    return max(count, 1)


def _difficulty_label(score: float) -> str:
    for threshold, label in _DIFFICULTY_BANDS:
        if score >= threshold:
            return label
    return "Very Difficult"  # unreachable (last band is -inf) but keeps mypy/pylint happy


async def analyze_readability(text: str) -> dict:
    """
    Compute Flesch Reading Ease and Flesch-Kincaid Grade Level for the
    input text.

    Args:
        text (str): Text to analyze (Pydantic layer already enforces a
            minimum length; this re-checks defensively).
    Returns:
        dict: word/sentence/syllable counts, both scores, and a
            human-readable difficulty label.
    Raises:
        ValueError: If text is empty/whitespace-only, too short, or
            contains no recognizable words.
    """
    if not text or not text.strip():
        logger.error("Text is empty")
        raise ValueError("Text cannot be empty")
    if len(text.strip()) < 20:
        logger.error("Text too short: %d characters", len(text.strip()))
        raise ValueError("Text must be at least 20 characters")

    words = re.findall(r"[A-Za-z']+", text)
    word_count = len(words)
    if word_count == 0:
        logger.error("No words found in text")
        raise ValueError("No words found in text")

    sentences = [s for s in re.split(r"[.!?]+", text) if s.strip()]
    sentence_count = len(sentences)
    # Guard against division-by-zero: text with no terminal punctuation
    # (e.g. a single clause with no ./!/?) still counts as one sentence.
    if sentence_count == 0:
        sentence_count = 1

    syllable_count = sum(_count_syllables(w) for w in words)

    words_per_sentence = word_count / sentence_count
    syllables_per_word = syllable_count / word_count

    flesch_reading_ease = round(
        206.835 - (1.015 * words_per_sentence) - (84.6 * syllables_per_word), 2
    )
    flesch_kincaid_grade = round(
        (0.39 * words_per_sentence) + (11.8 * syllables_per_word) - 15.59, 2
    )

    result = {
        "text": text,
        "word_count": word_count,
        "sentence_count": sentence_count,
        "syllable_count": syllable_count,
        "flesch_reading_ease": flesch_reading_ease,
        "flesch_kincaid_grade": flesch_kincaid_grade,
        "difficulty": _difficulty_label(flesch_reading_ease),
    }
    logger.debug("Readability result: %s", result)
    return result
