"""Tests for image generation module."""
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from lib.image_gen import ImageEvent, ImageGenerator, IMAGE_PROMPTS


class TestImageEvent:
    """Test ImageEvent enum coverage."""

    def test_all_events_have_prompts(self):
        """Every ImageEvent should have a corresponding prompt."""
        for event in ImageEvent:
            assert event in IMAGE_PROMPTS, f"Missing prompt for {event.value}"

    def test_prompts_are_nonempty(self):
        for event, prompt in IMAGE_PROMPTS.items():
            assert len(prompt) > 20, f"Prompt too short for {event.value}"


class TestImageGeneratorInit:
    """Test initialization and client setup."""

    def test_disabled_skips_generation(self):
        gen = ImageGenerator(enabled=False)
        result = gen.generate(ImageEvent.MERGE_COMPLETE)
        assert result is None

    def test_no_api_key_disables(self):
        gen = ImageGenerator()
        with patch.dict(os.environ, {}, clear=True):
            # Remove any existing keys
            os.environ.pop("GEMINI_API_KEY", None)
            os.environ.pop("GOOGLE_API_KEY", None)
            gen._available = None  # Reset cached state
            assert gen._ensure_client() is False
            assert gen._available is False

    def test_missing_package_disables(self):
        gen = ImageGenerator()
        gen._available = None
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            with patch("lib.image_gen.ImageGenerator._ensure_client") as mock_ensure:
                mock_ensure.return_value = False
                result = gen.generate(ImageEvent.PR_CREATED)
                assert result is None

    def test_cached_availability(self):
        gen = ImageGenerator()
        gen._available = True
        gen._client = MagicMock()
        # Should return True without re-checking
        assert gen._ensure_client() is True


class TestImageGeneratorGenerate:
    """Test image generation logic."""

    def test_generate_returns_path_on_success(self):
        gen = ImageGenerator()
        gen._available = True
        gen._client = MagicMock()

        # Mock the Gemini response chain
        mock_part = MagicMock()
        mock_part.inline_data = True
        mock_genai_image = MagicMock()
        mock_genai_image.image_bytes = b'\x00' * 100
        mock_part.as_image.return_value = mock_genai_image

        mock_response = MagicMock()
        mock_response.parts = [mock_part]
        gen._client.models.generate_content.return_value = mock_response

        # Mock PIL at import site (inside the generate method)
        mock_pil_image = MagicMock()
        with patch.dict("sys.modules", {"PIL": MagicMock(), "PIL.Image": MagicMock()}):
            import sys
            sys.modules["PIL.Image"].open.return_value = mock_pil_image

            # Also need to mock the types import
            mock_types = MagicMock()
            with patch.dict("sys.modules", {
                "google": MagicMock(),
                "google.genai": MagicMock(),
                "google.genai.types": mock_types,
                "PIL": MagicMock(),
                "PIL.Image": MagicMock(),
            }):
                pil_mod = sys.modules["PIL.Image"]
                pil_mod.open.return_value = mock_pil_image

                result = gen.generate(ImageEvent.MERGE_COMPLETE)

                assert result is not None
                assert isinstance(result, Path)
                assert "porter-merge_complete-" in str(result)
                # Clean up
                if result and result.exists():
                    os.unlink(result)

    def test_generate_returns_none_on_empty_response(self):
        gen = ImageGenerator()
        gen._available = True
        gen._client = MagicMock()

        mock_response = MagicMock()
        mock_response.parts = []
        gen._client.models.generate_content.return_value = mock_response

        result = gen.generate(ImageEvent.ERROR)
        assert result is None

    def test_generate_returns_none_on_exception(self):
        gen = ImageGenerator()
        gen._available = True
        gen._client = MagicMock()
        gen._client.models.generate_content.side_effect = Exception("API error")

        result = gen.generate(ImageEvent.TASK_RECEIVED)
        assert result is None

    def test_context_appended_to_prompt(self):
        gen = ImageGenerator()
        gen._available = True
        gen._client = MagicMock()

        mock_response = MagicMock()
        mock_response.parts = []
        gen._client.models.generate_content.return_value = mock_response

        gen.generate(ImageEvent.TASK_RECEIVED, context="Add dark mode")

        call_args = gen._client.models.generate_content.call_args
        prompt = call_args.kwargs.get("contents", call_args[1].get("contents", [None]))[0]
        assert "Add dark mode" in prompt


class TestNotifierImageIntegration:
    """Test that Notifier correctly uses ImageGenerator."""

    def test_send_with_image_path(self):
        from lib.notifier import Notifier

        notifier = Notifier("/usr/bin/true", "+1234567890")

        with tempfile.NamedTemporaryFile(suffix=".webp", delete=False) as f:
            f.write(b"fake image")
            tmp_path = Path(f.name)

        try:
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                notifier.send("test message", image_path=tmp_path)

                cmd = mock_run.call_args[0][0]
                assert "--file" in cmd
                assert str(tmp_path) in cmd
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_send_without_image(self):
        from lib.notifier import Notifier

        notifier = Notifier("/usr/bin/true", "+1234567890")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            notifier.send("test message")

            cmd = mock_run.call_args[0][0]
            assert "--file" not in cmd

    def test_send_with_image_cleans_up_temp_file(self):
        from lib.notifier import Notifier

        mock_gen = MagicMock(spec=ImageGenerator)
        tmp = tempfile.NamedTemporaryFile(suffix=".webp", delete=False)
        tmp.write(b"fake")
        tmp.close()
        mock_gen.generate.return_value = Path(tmp.name)

        notifier = Notifier("/usr/bin/true", "+1234567890", image_generator=mock_gen)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            notifier._send_with_image("test", ImageEvent.MERGE_COMPLETE)

        # Temp file should be cleaned up
        assert not Path(tmp.name).exists()

    def test_send_with_image_fallback_on_none(self):
        from lib.notifier import Notifier

        mock_gen = MagicMock(spec=ImageGenerator)
        mock_gen.generate.return_value = None

        notifier = Notifier("/usr/bin/true", "+1234567890", image_generator=mock_gen)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            notifier._send_with_image("test", ImageEvent.ERROR)

            cmd = mock_run.call_args[0][0]
            assert "--file" not in cmd  # No image, just text

    def test_notify_merge_complete_uses_image(self):
        from lib.notifier import Notifier

        mock_gen = MagicMock(spec=ImageGenerator)
        mock_gen.generate.return_value = None

        notifier = Notifier("/usr/bin/true", "+1234567890", image_generator=mock_gen)

        task = MagicMock()
        task.task_title = "Add dark mode"
        task.id = "test-123"

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            notifier.notify_merge_complete(task)

        mock_gen.generate.assert_called_once()
        call_args = mock_gen.generate.call_args
        assert call_args[0][0] == ImageEvent.MERGE_COMPLETE
