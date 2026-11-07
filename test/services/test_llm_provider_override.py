"""Choosing the script model per run instead of per config file.

`[app] llm_provider` is one global setting. These tests cover the per-run
override that sits on top of it: the CLI flag, the API field, and the config
snapshot the two LLM stages are handed so that one run cannot end up half on
Ollama and half on Claude.

    python -m pytest test/services/test_llm_provider_override.py
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import cli  # noqa: E402
from app.models.const import DEFAULT_VIDEO_SOURCE  # noqa: E402
from app.models.llm_provider import (  # noqa: E402
    DEFAULT_LLM_PROVIDER_ID,
    LLM_PROVIDER_REGISTRY,
    LLM_PROVIDERS,
)
from app.models.schema import (  # noqa: E402
    VideoParams,
    VideoScriptRequest,
    VideoTermsRequest,
)
from app.services import llm  # noqa: E402


class DefaultsAreLocal(unittest.TestCase):
    """A fresh install should generate a video without an account anywhere."""

    def test_default_llm_provider_is_ollama(self):
        self.assertEqual(DEFAULT_LLM_PROVIDER_ID, "ollama")

    def test_default_provider_is_in_the_registry(self):
        self.assertIn(DEFAULT_LLM_PROVIDER_ID, LLM_PROVIDERS)

    def test_ollama_needs_no_api_key(self):
        """Otherwise the default would fail the preflight on a clean install."""
        self.assertFalse(LLM_PROVIDERS["ollama"].requires_api_key)

    def test_selector_lists_ollama_first_then_the_claude_api(self):
        ids = [provider.provider_id for provider in LLM_PROVIDER_REGISTRY]
        self.assertEqual(ids[:3], ["ollama", "anthropic", "claude_code"])

    def test_every_provider_survives_the_reordering(self):
        """Reordering the registry must not drop or duplicate a provider."""
        ids = [provider.provider_id for provider in LLM_PROVIDER_REGISTRY]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), len(LLM_PROVIDERS))
        for expected in ("moonshot", "openai", "azure", "gemini", "deepseek", "litellm"):
            self.assertIn(expected, ids)

    def test_default_video_source_is_the_local_image_server(self):
        self.assertEqual(DEFAULT_VIDEO_SOURCE, "openai_image")
        self.assertEqual(VideoParams(video_subject="x").video_source, "openai_image")

    def test_default_voice_is_a_local_kokoro_voice(self):
        self.assertTrue(VideoParams(video_subject="x").voice_name.startswith("kokoro:"))


class ResolveLlmAppConfig(unittest.TestCase):
    CONFIG = {
        "llm_provider": "moonshot",
        "moonshot_api_key": "configured-moonshot-key",
        "moonshot_model_name": "configured-moonshot-model",
        "anthropic_api_key": "configured-anthropic-key",
        "anthropic_model_name": "configured-anthropic-model",
        "ollama_model_name": "qwen3:14b",
    }

    def test_config_is_used_when_no_override_is_given(self):
        for empty in ("", None, "   "):
            with self.subTest(override=empty):
                resolved = llm.resolve_llm_app_config(empty, empty, app_config=self.CONFIG)
                self.assertEqual(resolved["llm_provider"], "moonshot")

    def test_override_is_used_when_given(self):
        resolved = llm.resolve_llm_app_config("anthropic", app_config=self.CONFIG)
        self.assertEqual(resolved["llm_provider"], "anthropic")
        # The rest of the config comes along, so the key is still found.
        self.assertEqual(resolved["anthropic_api_key"], "configured-anthropic-key")

    def test_override_is_case_insensitive(self):
        resolved = llm.resolve_llm_app_config("Anthropic", app_config=self.CONFIG)
        self.assertEqual(resolved["llm_provider"], "anthropic")

    def test_model_override_targets_the_chosen_provider(self):
        resolved = llm.resolve_llm_app_config(
            "anthropic", "claude-opus-4-5", app_config=self.CONFIG
        )
        self.assertEqual(resolved["anthropic_model_name"], "claude-opus-4-5")
        # and leaves the other providers' models alone
        self.assertEqual(resolved["moonshot_model_name"], "configured-moonshot-model")

    def test_model_override_without_a_provider_targets_the_configured_one(self):
        resolved = llm.resolve_llm_app_config("", "kimi-k2", app_config=self.CONFIG)
        self.assertEqual(resolved["llm_provider"], "moonshot")
        self.assertEqual(resolved["moonshot_model_name"], "kimi-k2")

    def test_the_caller_config_is_not_mutated(self):
        """Two concurrent tasks must not be able to change each other's provider."""
        original = dict(self.CONFIG)
        resolved = llm.resolve_llm_app_config(
            "anthropic", "claude-opus-4-5", app_config=self.CONFIG
        )
        self.assertEqual(self.CONFIG, original)
        self.assertIsNot(resolved, self.CONFIG)

    def test_unknown_provider_id_gives_a_clear_error(self):
        with self.assertRaises(ValueError) as caught:
            llm.resolve_llm_app_config("gpt5-turbo-ultra", app_config=self.CONFIG)
        message = str(caught.exception)
        self.assertIn("gpt5-turbo-ultra", message)
        self.assertIn("not a known llm provider", message)
        # The message lists what there is, rather than only what there is not.
        self.assertIn("ollama", message)
        self.assertIn("anthropic", message)

    def test_unknown_provider_in_config_also_gives_a_clear_error(self):
        with self.assertRaises(ValueError) as caught:
            llm.resolve_llm_app_config(app_config={"llm_provider": "not-a-provider"})
        self.assertIn("not a known llm provider", str(caught.exception))

    def test_missing_anthropic_key_fails_the_preflight(self):
        """Step one of the pipeline, not after the pictures have been made."""
        with self.assertRaises(ValueError) as caught:
            llm.resolve_llm_app_config(
                "anthropic",
                app_config={"anthropic_api_key": "", "anthropic_model_name": "m"},
                require_credentials=True,
            )
        message = str(caught.exception)
        self.assertIn("anthropic", message)
        self.assertIn("api_key is not set", message)

    def test_a_present_anthropic_key_passes_the_preflight(self):
        resolved = llm.resolve_llm_app_config(
            "anthropic", app_config=self.CONFIG, require_credentials=True
        )
        self.assertEqual(resolved["llm_provider"], "anthropic")

    def test_ollama_passes_the_preflight_with_no_key_at_all(self):
        resolved = llm.resolve_llm_app_config(
            "ollama",
            app_config={"ollama_model_name": "qwen3:14b"},
            require_credentials=True,
        )
        self.assertEqual(resolved["llm_provider"], "ollama")

    def test_the_preflight_checks_credentials_and_not_settings(self):
        """A missing model name must not stop the pipeline before it starts.

        It is a setting, reported clearly at the point of use, and it is empty
        on every fresh install until the user picks a model. Only a missing API
        key — expensive to discover after the narration and pictures are
        made — is worth failing early for.
        """
        resolved = llm.resolve_llm_app_config(
            "ollama", app_config={"ollama_model_name": ""}, require_credentials=True
        )
        self.assertEqual(resolved["llm_provider"], "ollama")

    def test_credentials_are_not_checked_unless_asked(self):
        """A run that brings its own script and keywords needs no key."""
        resolved = llm.resolve_llm_app_config(
            "anthropic", app_config={"anthropic_api_key": ""}
        )
        self.assertEqual(resolved["llm_provider"], "anthropic")

    def test_an_absent_llm_provider_key_falls_back_to_the_default(self):
        """A config.toml written before this setting existed still works."""
        resolved = llm.resolve_llm_app_config(app_config={})
        self.assertEqual(resolved["llm_provider"], DEFAULT_LLM_PROVIDER_ID)

    def test_a_blank_llm_provider_falls_back_to_the_default(self):
        """Blank means "no preference", not "no provider"."""
        for blank in ("", "   ", None):
            with self.subTest(configured=blank):
                resolved = llm.resolve_llm_app_config(app_config={"llm_provider": blank})
                self.assertEqual(resolved["llm_provider"], DEFAULT_LLM_PROVIDER_ID)

    def test_the_snapshot_always_states_its_provider(self):
        """Downstream reads the snapshot, so it must not merely imply one.

        `unload_ollama_model` decides from this key whether there is anything
        to unload; a snapshot without it would have to guess.
        """
        for app_config in ({}, {"llm_provider": ""}, {"llm_provider": "moonshot"}):
            with self.subTest(app_config=app_config):
                resolved = llm.resolve_llm_app_config(app_config=app_config)
                self.assertIn("llm_provider", resolved)
                self.assertIn(resolved["llm_provider"], LLM_PROVIDERS)

    def test_it_works_against_the_real_config_section(self):
        """config.app is a synchronized section, not a plain dict."""
        resolved = llm.resolve_llm_app_config()
        self.assertIn(resolved["llm_provider"], LLM_PROVIDERS)
        self.assertIsInstance(resolved, dict)


class OverrideReachesTheModel(unittest.TestCase):
    """The snapshot has to arrive at the call, not just be built correctly."""

    def _capture(self, call):
        seen = {}

        def fake_generate_response(prompt, app_config=None, **kwargs):
            seen["app_config"] = app_config
            return '["one", "two", "three", "four", "five"]'

        with patch.object(llm, "_generate_response", side_effect=fake_generate_response):
            call()
        return seen

    def test_generate_script_uses_the_snapshot(self):
        snapshot = llm.resolve_llm_app_config(
            "anthropic", app_config={"llm_provider": "ollama", "anthropic_api_key": "k"}
        )
        seen = self._capture(
            lambda: llm.generate_script(video_subject="iron", app_config=snapshot)
        )
        self.assertEqual(seen["app_config"]["llm_provider"], "anthropic")

    def test_generate_terms_uses_the_snapshot(self):
        snapshot = llm.resolve_llm_app_config(
            "anthropic", app_config={"llm_provider": "ollama", "anthropic_api_key": "k"}
        )
        seen = self._capture(
            lambda: llm.generate_terms(
                video_subject="iron", video_script="a script", app_config=snapshot
            )
        )
        self.assertEqual(seen["app_config"]["llm_provider"], "anthropic")


class UnloadingOllama(unittest.TestCase):
    """Only Ollama holds VRAM between stages, so only Ollama is unloaded."""

    OLLAMA = {"llm_provider": "ollama", "ollama_model_name": "qwen3:14b"}

    def test_ollama_is_asked_to_unload(self):
        with patch.object(llm.requests, "post") as post:
            self.assertTrue(llm.unload_ollama_model(self.OLLAMA))
        post.assert_called_once()
        url = post.call_args.args[0]
        self.assertTrue(url.endswith("/api/generate"), url)
        self.assertEqual(post.call_args.kwargs["json"]["keep_alive"], 0)
        self.assertEqual(post.call_args.kwargs["json"]["model"], "qwen3:14b")

    def test_a_v1_base_url_is_trimmed_back_to_the_native_api(self):
        config = {**self.OLLAMA, "ollama_base_url": "http://10.0.0.5:11434/v1"}
        with patch.object(llm.requests, "post") as post:
            llm.unload_ollama_model(config)
        self.assertEqual(
            post.call_args.args[0], "http://10.0.0.5:11434/api/generate"
        )

    def test_a_claude_run_does_not_unload_anything(self):
        for provider in ("anthropic", "claude_code", "openai", "moonshot"):
            with self.subTest(provider=provider):
                with patch.object(llm.requests, "post") as post:
                    sent = llm.unload_ollama_model(
                        {"llm_provider": provider, "ollama_model_name": "qwen3:14b"}
                    )
                self.assertFalse(sent)
                post.assert_not_called()

    def test_no_model_name_means_nothing_to_unload(self):
        with patch.object(llm.requests, "post") as post:
            self.assertFalse(
                llm.unload_ollama_model({"llm_provider": "ollama", "ollama_model_name": ""})
            )
        post.assert_not_called()

    def test_an_unreachable_ollama_is_not_a_failed_video(self):
        with patch.object(llm.requests, "post", side_effect=OSError("refused")):
            self.assertFalse(llm.unload_ollama_model(self.OLLAMA))


class CliFlags(unittest.TestCase):
    BASE = ["--video-subject", "iron"]

    def test_llm_provider_defaults_to_none_so_config_decides(self):
        args = cli.parse_args(self.BASE)
        self.assertIsNone(args.llm_provider)
        self.assertIsNone(args.llm_model)

    def test_llm_provider_and_model_are_parsed(self):
        args = cli.parse_args(
            self.BASE + ["--llm-provider", "anthropic", "--llm-model", "claude-opus-4-5"]
        )
        self.assertEqual(args.llm_provider, "anthropic")
        self.assertEqual(args.llm_model, "claude-opus-4-5")

    def test_every_registry_provider_is_accepted(self):
        for provider_id in LLM_PROVIDERS:
            with self.subTest(provider=provider_id):
                args = cli.parse_args(self.BASE + ["--llm-provider", provider_id])
                self.assertEqual(args.llm_provider, provider_id)

    def test_an_unknown_provider_is_refused_by_argparse(self):
        with self.assertRaises(SystemExit):
            cli.parse_args(self.BASE + ["--llm-provider", "not-a-provider"])

    def test_video_source_defaults_to_the_local_image_server(self):
        self.assertEqual(cli.parse_args(self.BASE).video_source, DEFAULT_VIDEO_SOURCE)


class ApiFields(unittest.TestCase):
    def test_task_params_carry_the_override(self):
        params = VideoParams(
            video_subject="iron", llm_provider="anthropic", llm_model="claude-opus-4-5"
        )
        self.assertEqual(params.llm_provider, "anthropic")
        self.assertEqual(params.llm_model, "claude-opus-4-5")

    def test_task_params_default_to_empty_so_config_decides(self):
        params = VideoParams(video_subject="iron")
        self.assertEqual(params.llm_provider, "")
        self.assertEqual(params.llm_model, "")

    def test_the_script_and_terms_endpoints_carry_it_too(self):
        script = VideoScriptRequest(video_subject="iron", llm_provider="ollama")
        terms = VideoTermsRequest(
            video_subject="iron", video_script="s", llm_provider="ollama"
        )
        self.assertEqual(script.llm_provider, "ollama")
        self.assertEqual(terms.llm_provider, "ollama")


if __name__ == "__main__":
    unittest.main()
