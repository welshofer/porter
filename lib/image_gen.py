"""
Porter — Image generation for rich iMessage notifications.

Uses the Gemini API (google-genai) to generate contextual images
for task lifecycle events. Gracefully degrades when GEMINI_API_KEY
is not set — notifications fall back to text-only.
"""

import io
import logging
import os
import tempfile
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger('porter')


class ImageEvent(str, Enum):
    """Events that can trigger image generation."""
    TASK_RECEIVED = "task_received"
    WORK_STARTING = "work_starting"
    PR_CREATED = "pr_created"
    MERGE_COMPLETE = "merge_complete"
    ERROR = "error"
    RETRY = "retry"
    TESTS_PASSED = "tests_passed"
    TESTS_FAILED = "tests_failed"


# Prompt templates for each event type.
# Each prompt generates a fun, expressive banana character in different situations.
# The banana theme ties into the nano-banana lineage of the image generation.
IMAGE_PROMPTS = {
    ImageEvent.TASK_RECEIVED: (
        "A cute cartoon banana character wearing a hard hat and reading a blueprint, "
        "looking excited and determined. Clean white background, kawaii style, "
        "bright cheerful colors. The banana is giving a thumbs up."
    ),
    ImageEvent.WORK_STARTING: (
        "A cute cartoon banana character sitting at a desk with multiple monitors "
        "showing code, wearing headphones and looking focused. Clean white background, "
        "kawaii style. Coffee cup nearby. The banana looks ready to work."
    ),
    ImageEvent.PR_CREATED: (
        "A cute cartoon banana character proudly presenting a finished document "
        "with a green checkmark on it. Clean white background, kawaii style. "
        "Sparkles and stars around the banana. Celebrating accomplishment."
    ),
    ImageEvent.MERGE_COMPLETE: (
        "A cute cartoon banana character doing a victory dance with confetti and "
        "streamers falling around it. Clean white background, kawaii style. "
        "Party hat on, huge smile, arms raised in celebration. 🎉 energy."
    ),
    ImageEvent.ERROR: (
        "A cute cartoon banana character looking frustrated and confused, "
        "with sweat drops and a small storm cloud above its head. "
        "Clean white background, kawaii style. The banana is scratching its head."
    ),
    ImageEvent.RETRY: (
        "A cute cartoon banana character rolling up its sleeves with a determined "
        "expression, ready to try again. Clean white background, kawaii style. "
        "A small 'round 2' sign nearby. Resilient energy."
    ),
    ImageEvent.TESTS_PASSED: (
        "A cute cartoon banana character in a lab coat holding a beaker with a "
        "green glowing liquid, surrounded by green checkmarks. Clean white background, "
        "kawaii style. Proud scientist energy."
    ),
    ImageEvent.TESTS_FAILED: (
        "A cute cartoon banana character in a lab coat looking worried as a beaker "
        "overflows with red liquid. Clean white background, kawaii style. "
        "Small red X marks floating around. Determined to fix it."
    ),
}


class ImageGenerator:
    """Generates contextual images for task notifications using Gemini."""

    def __init__(self, model: str = "gemini-2.5-flash-image", enabled: bool = True):
        self.model = model
        self.enabled = enabled
        self._client = None
        self._available = None  # None = not checked yet

    def _ensure_client(self) -> bool:
        """Lazily initialize the Gemini client. Returns True if available."""
        if self._available is not None:
            return self._available

        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            logger.info("No GEMINI_API_KEY or GOOGLE_API_KEY set — image generation disabled")
            self._available = False
            return False

        try:
            from google import genai
            base_url = os.environ.get("GOOGLE_GEMINI_BASE_URL")
            if base_url:
                self._client = genai.Client(api_key=api_key, http_options={'base_url': base_url})
            else:
                self._client = genai.Client(api_key=api_key)
            self._available = True
            logger.info("Gemini image generation initialized")
            return True
        except ImportError:
            logger.warning("google-genai package not installed — image generation disabled")
            self._available = False
            return False
        except Exception as e:
            logger.warning(f"Failed to initialize Gemini client: {e}")
            self._available = False
            return False

    def generate(self, event: ImageEvent, context: str = "") -> Optional[Path]:
        """Generate an image for the given event. Returns path to temp file, or None on failure.

        The caller is responsible for cleaning up the temp file after use.
        """
        if not self.enabled or not self._ensure_client():
            return None

        prompt = IMAGE_PROMPTS.get(event)
        if not prompt:
            return None

        # Append task-specific context to the prompt
        if context:
            prompt = f"{prompt} Context: {context}"

        try:
            from google.genai import types
            from PIL import Image as PILImage

            response = self._client.models.generate_content(
                model=self.model,
                contents=[prompt],
                config=types.GenerateContentConfig(
                    response_modalities=['IMAGE']
                )
            )

            if not response.parts:
                logger.warning(f"No image generated for event {event.value}")
                return None

            for part in response.parts:
                if part.inline_data is not None:
                    genai_image = part.as_image()
                    pil_image = PILImage.open(io.BytesIO(genai_image.image_bytes))

                    # Save to temp file (caller cleans up)
                    tmp = tempfile.NamedTemporaryFile(
                        suffix=".webp", prefix=f"porter-{event.value}-", delete=False
                    )
                    pil_image.save(tmp.name, "WEBP", quality=85)
                    tmp.close()
                    logger.info(f"Generated image for {event.value}: {tmp.name}")
                    return Path(tmp.name)

            logger.warning(f"Response had parts but no image data for event {event.value}")
            return None

        except Exception as e:
            logger.warning(f"Image generation failed for {event.value}: {e}")
            return None
