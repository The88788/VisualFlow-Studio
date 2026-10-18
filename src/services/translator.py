"""Translation service using Yandex Translate API."""

from typing import Optional

import httpx

from ..core.config import config
from ..core.logger import logger


class TranslatorService:
    """Yandex Translate API integration for prompt translation."""

    def __init__(self):
        self._api_key = config.yandex_translate_api_key
        self._folder_id = config.yandex_translate_folder_id

    @property
    def is_configured(self) -> bool:
        return bool(self._api_key and self._folder_id)

    async def detect_and_translate(
        self, text: str
    ) -> tuple[str, Optional[str], str]:
        """
        Detect language and translate to English if needed.

        Returns:
            (translated_text, detected_language, original_text)
            If translation is not configured or text is already English,
            returns (original_text, None, original_text).
        """

        if not self.is_configured:
            # Fallback to translatepy (YandexTranslate)
            try:
                from translatepy.translators.yandex import YandexTranslate
                translator = YandexTranslate()
                
                # Detect language (optional, translatepy does this internally usually but we want to return it)
                # translatepy doesn't always expose detection easily in one go, but we can try to assume
                # or just translate. Let's just translate to English.
                
                result = translator.translate(text, "English")
                translated_text = result.result
                detected_lang = result.source_language.id  # e.g. "ru"
                
                if not detected_lang or detected_lang == "en":
                     return text, detected_lang, text

                logger.info(f"[Fallback] Translated prompt from '{detected_lang}' to 'en'")
                return translated_text, detected_lang, text
                
            except Exception as e:
                logger.warning(f"Fallback translation failed: {e}")
                return text, None, text

        try:
            # Detect language
            detected_lang = await self._detect_language(text)

            if not detected_lang or detected_lang == "en":
                return text, detected_lang, text

            # Translate to English
            translated = await self._translate(text, detected_lang, "en")
            if translated:
                logger.info(
                    f"Translated prompt from '{detected_lang}' to 'en'"
                )
                return translated, detected_lang, text

            return text, detected_lang, text

        except Exception as e:
            logger.error(f"Translation failed: {e}")
            return text, None, text

    async def _detect_language(self, text: str) -> Optional[str]:
        """Detect the language of text using Yandex Translate API."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    "https://translate.api.cloud.yandex.net/translate/v2/detect",
                    headers={
                        "Authorization": f"Api-Key {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "folderId": self._folder_id,
                        "text": text[:1000],
                    },
                )

                if response.status_code == 200:
                    data = response.json()
                    return data.get("languageCode")

                logger.warning(
                    f"Language detection failed: {response.status_code}"
                )
                return None

        except Exception as e:
            logger.error(f"Language detection error: {e}")
            return None

    async def _translate(
        self, text: str, source_lang: str, target_lang: str
    ) -> Optional[str]:
        """Translate text using Yandex Translate API."""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.post(
                    "https://translate.api.cloud.yandex.net/translate/v2/translate",
                    headers={
                        "Authorization": f"Api-Key {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "folderId": self._folder_id,
                        "texts": [text],
                        "sourceLanguageCode": source_lang,
                        "targetLanguageCode": target_lang,
                    },
                )

                if response.status_code == 200:
                    data = response.json()
                    translations = data.get("translations", [])
                    if translations:
                        return translations[0].get("text")

                logger.warning(
                    f"Translation failed: {response.status_code}"
                )
                return None

        except Exception as e:
            logger.error(f"Translation error: {e}")
            return None


# Global instance
translator = TranslatorService()
