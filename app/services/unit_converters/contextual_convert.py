import nltk
from nltk.tokenize import word_tokenize
import logging

logger = logging.getLogger(__name__)
nltk.download('punkt', quiet=True)

async def contextual_convert(query: str) -> dict:
    """
    Convert units based on a natural language query.
    Args:
        query (str): Query like "convert 5 feet to meters".
    Returns:
        dict: Conversion result.
    Raises:
        ValueError: If query is invalid or units unsupported.
    """
    units = {
        "meter": 1.0,  # Base unit
        "meters": 1.0,
        "foot": 0.3048,  # 1 meter = 0.3048 feet
        "feet": 0.3048,
        "inch": 0.0254,  # 1 meter = 0.0254 inches
        "inches": 0.0254,
        "kilometer": 1000.0,  # 1 meter = 1000 kilometers
        "kilometers": 1000.0
    }
    try:
        tokens = word_tokenize(query.lower())
        value = None
        from_unit = None
        to_unit = None
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token.isdigit() or (token.replace(".", "").isdigit() and "." in token):
                value = float(token)
            elif token in units:
                if from_unit is None:
                    from_unit = token
                else:
                    to_unit = token
            i += 1
        logger.debug("Parsed: value=%s, from_unit=%s, to_unit=%s", value, from_unit, to_unit)
        if value is None or from_unit is None or to_unit is None:
            logger.error("Invalid query: %s, parsed: value=%s, from_unit=%s, to_unit=%s", query, value, from_unit, to_unit)
            raise ValueError("Query must include a value and two units")
        if from_unit not in units or to_unit not in units:
            logger.error("Unsupported units: %s, %s", from_unit, to_unit)
            raise ValueError("Unsupported units")
        result = value * units[from_unit] / units[to_unit]
        logger.debug("Conversion result: %s %s = %s %s", value, from_unit, result, to_unit)
        return {
            "query": query,
            "value": value,
            "from_unit": from_unit,
            "to_unit": to_unit,
            "result": round(result, 4)
        }
    except Exception as e:
        logger.exception("Error converting units: %s", str(e))
        raise