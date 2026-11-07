"""The all-local pipeline must not reach a cloud service without being asked.

The pipeline has one provider per stage and no fallback between them, which is
what makes an offline setup trustworthy. These tests pin the places where that
could quietly stop being true.

    python -m pytest test/services/test_local_path_no_cloud.py
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.services import voice  # noqa: E402


class AzureIsNotTheCatchAll(unittest.TestCase):
    """A voice name that matches no provider used to be sent to Microsoft.

    Edge TTS was once the only provider, so the dispatcher ended in an
    unconditional call to it. With twelve providers and prefix-based routing, a
    name that lost its prefix — "af_heart" rather than "kokoro:af_heart" — hit
    that branch, and an offline pipeline made a silent network request.
    """

    def _dispatch(self, voice_name):
        with patch.object(voice, "azure_tts_v1") as azure:
            azure.return_value = "azure-was-called"
            result = voice._single_tts(
                text="iron was hammered cold",
                voice_name=voice_name,
                voice_rate=1.0,
                voice_file="unused.mp3",
            )
        return result, azure.called

    def test_a_bare_kokoro_voice_name_does_not_reach_azure(self):
        result, azure_called = self._dispatch("af_heart")
        self.assertFalse(azure_called)
        self.assertIsNone(result)

    def test_other_unrecognised_names_do_not_reach_azure(self):
        for name in ("af_heart-Female", "default", "nonsense", "en-US-Ava", "zh-CN"):
            with self.subTest(voice_name=name):
                result, azure_called = self._dispatch(name)
                self.assertFalse(azure_called)
                self.assertIsNone(result)

    def test_a_real_edge_voice_still_reaches_azure(self):
        """Edge TTS stays available — it just is not the fallback any more."""
        for name in (
            "en-US-AvaNeural",
            "zh-CN-XiaoxiaoNeural-Female",
            "en-US-AvaMultilingualNeural",
            "en-GB-SoniaNeural-Male",
        ):
            with self.subTest(voice_name=name):
                result, azure_called = self._dispatch(name)
                self.assertTrue(azure_called)
                self.assertEqual(result, "azure-was-called")

    def test_a_kokoro_voice_goes_to_kokoro(self):
        with patch.object(voice, "kokoro_tts") as kokoro, patch.object(
            voice, "azure_tts_v1"
        ) as azure:
            kokoro.return_value = "kokoro-was-called"
            result = voice._single_tts(
                text="iron was hammered cold",
                voice_name="kokoro:af_heart",
                voice_rate=1.0,
                voice_file="unused.mp3",
            )
        self.assertEqual(result, "kokoro-was-called")
        self.assertFalse(azure.called)
        self.assertEqual(kokoro.call_args.args[1], "af_heart")

    def test_the_voice_shape_check_is_a_positive_one(self):
        self.assertTrue(voice.looks_like_azure_v1_voice("en-US-AvaNeural"))
        self.assertFalse(voice.looks_like_azure_v1_voice("af_heart"))
        self.assertFalse(voice.looks_like_azure_v1_voice(""))
        self.assertFalse(voice.looks_like_azure_v1_voice(None))


class KokoroFailsRatherThanFallsBack(unittest.TestCase):
    def test_no_base_url_is_an_error_not_a_different_provider(self):
        with patch.dict(voice.config.kokoro, {"base_url": ""}, clear=False):
            with patch.object(voice, "_openai_compatible_tts") as transport:
                result = voice.kokoro_tts("iron", "af_heart", "unused.mp3")
        self.assertIsNone(result)
        self.assertFalse(transport.called)

    def test_the_request_goes_to_the_configured_server_only(self):
        with patch.dict(
            voice.config.kokoro, {"base_url": "http://127.0.0.1:8880/v1"}, clear=False
        ):
            with patch.object(voice, "_openai_compatible_tts") as transport:
                transport.return_value = "ok"
                voice.kokoro_tts("iron", "af_heart", "unused.mp3")
        provider, base_url = transport.call_args.args[0], transport.call_args.args[1]
        self.assertEqual(provider, "kokoro")
        self.assertEqual(base_url, "http://127.0.0.1:8880/v1")


if __name__ == "__main__":
    unittest.main()
