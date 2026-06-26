from deep_translator import GoogleTranslator

LANGUAGES = {
    "hindi": "hi",
    "tamil": "ta",
    "telugu": "te",
    "bengali": "bn",
    "marathi": "mr",
    "gujarati": "gu",
    "kannada": "kn",
    "malayalam": "ml",
    "punjabi": "pa",
    "urdu": "ur",
}


def translate_item(item: dict, language: str = "hindi") -> dict:
    lang_code = LANGUAGES.get(language, "hi")
    translator = GoogleTranslator(source="en", target=lang_code)
    return {
        **item,
        "language": language,
        "title": translator.translate(item["title"]),
        "summary": translator.translate(item["summary"]) if item["summary"] else "",
    }
