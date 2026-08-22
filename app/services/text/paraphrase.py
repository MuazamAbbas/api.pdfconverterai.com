import logging

logger = logging.getLogger(__name__)


async def paraphrase_text(text: str, pipeline) -> str:
    """Paraphrase input text using a preloaded t5-small pipeline.

    Args:
        text (str): Input text to paraphrase.
        pipeline: Preloaded Hugging Face `text2text-generation` pipeline
            (t5-small), loaded once per worker process in `app/worker.py`'s
            `on_startup` hook and passed in via `ctx["paraphrase_pipeline"]`
            (Handbook Part C.2/C.4, ADR-003/ADR-006) - this module no
            longer loads its own model or reads it off `app.state`
            (previously reached via a `Paraphraser(app)` instance in the
            FastAPI request process; that path is now Tier 2, so the
            pipeline lives in the ARQ worker process's `ctx` instead - see
            `app/services/text/processors.py`'s `TextParaphraseProcessor`).

    Returns:
        str: Paraphrased text.

    Raises:
        ValueError: If text is empty or not a string.

    The retry-with-alternative-prompt logic (paraphrase -> rephrase if the
    model echoes the input back unchanged) is preserved exactly from the
    prior `Paraphraser.paraphrase` implementation.
    """
    logger.debug("🔧 Paraphrasing text: %s", text)
    try:
        if not text or not isinstance(text, str):
            logger.error("❌ Invalid input: text must be a non-empty string")
            raise ValueError("Text must be a non-empty string")
        # Use precise prompt for t5-small
        input_text = f"paraphrase: {text}"
        logger.debug("📡 Sending text to paraphrase pipeline: %s", input_text)
        logger.debug(
            "Pipeline config: max_length=100, min_length=10, num_beams=4, "
            "no_repeat_ngram_size=2, length_penalty=1.0"
        )
        result = pipeline(
            input_text,
            max_length=100,
            min_length=10,
            num_beams=4,
            no_repeat_ngram_size=2,
            length_penalty=1.0,
            num_return_sequences=1,
            early_stopping=True
        )
        paraphrased = result[0]['generated_text'].strip()
        # Strip known prefixes
        prefixes = ["paraphrase:", "not_duplicate:", "rephrase:", "Paraphrase the following sentence in English:"]
        for prefix in prefixes:
            if paraphrased.startswith(prefix):
                paraphrased = paraphrased[len(prefix):].strip()
        # Fallback: if output is too similar to input, retry with alternative prompt
        if paraphrased.lower() == text.lower():
            logger.debug("🔄 Output matches input, retrying with alternative prompt")
            input_text = f"rephrase: {text}"
            result = pipeline(
                input_text,
                max_length=100,
                min_length=10,
                num_beams=4,
                no_repeat_ngram_size=2,
                length_penalty=1.0,
                num_return_sequences=1,
                early_stopping=True
            )
            paraphrased = result[0]['generated_text'].strip()
            for prefix in prefixes:
                if paraphrased.startswith(prefix):
                    paraphrased = paraphrased[len(prefix):].strip()
        logger.debug("✅ Paraphrased text: %s", paraphrased)
        return paraphrased
    except Exception as e:
        logger.exception("💥 Error paraphrasing text: %s", str(e))
        raise
