import logging
from fastapi import FastAPI
from transformers import pipeline
from transformers import __version__ as transformers_version
from app.core.config import settings

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("/home/pdfconverterai-api/htdocs/api.pdfconverterai.com/logs/error.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class Paraphraser:
    def __init__(self, app: FastAPI):
        logger.debug("🚀 Initializing Paraphraser")
        logger.debug("Transformers version: %s", transformers_version)
        try:
            if not hasattr(app.state, 'paraphrase_pipeline'):
                logger.error("❌ paraphrase_pipeline not found in app.state")
                raise AttributeError("Paraphrase pipeline not initialized")
            self.pipeline = app.state.paraphrase_pipeline
            logger.debug("✅ Paraphraser initialized with pipeline: %s", self.pipeline)
            logger.debug("Pipeline model: %s", getattr(self.pipeline, 'model', 'Unknown'))
        except Exception as e:
            logger.exception("💥 Failed to initialize Paraphraser: %s", str(e))
            raise

    async def paraphrase(self, text: str) -> str:
        logger.debug("🔧 Paraphrasing text: %s", text)
        try:
            if not text or not isinstance(text, str):
                logger.error("❌ Invalid input: text must be a non-empty string")
                raise ValueError("Text must be a non-empty string")
            # Use precise prompt for t5-small
            input_text = f"paraphrase: {text}"
            logger.debug("📡 Sending text to paraphrase pipeline: %s", input_text)
            logger.debug("Pipeline config: max_length=100, min_length=10, num_beams=4, no_repeat_ngram_size=2, length_penalty=1.0")
            result = self.pipeline(
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
                result = self.pipeline(
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