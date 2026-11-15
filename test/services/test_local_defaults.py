"""没设过的时候默认用本机的服务——而且**只**在没设过的时候。

背景：一份缺了出图地址和 Ollama 模型的 config.toml 开不了片子，用户得自己
去设置里把本机地址填一遍。这里钉住的是补默认值这件事的边界：

* 缺的补成本机的值；
* 已经写了值的一个字不动，哪怕它指向别处；
* 出图地址和模型一起补或者都不补；
* Ollama 的模型从它实际装了的里面挑，连不上就不补、照实说；
* 什么都不缺就不碰 config.toml。

Missing settings default to this machine's services, and only missing ones: a
value that is already there is never touched, the image address and model are
filled together or not at all, the Ollama model is chosen from what is actually
installed, and nothing is written when nothing is missing.
"""

from unittest.mock import patch

import pytest

from app.config import config
from app.models import const
from app.services import local_defaults

LOCAL_IMAGE = {
    "openai_image_base_url": const.DEFAULT_OPENAI_IMAGE_BASE_URL,
    "openai_image_model": const.DEFAULT_OPENAI_IMAGE_MODEL,
}


@pytest.fixture
def ollama():
    """Ollama 装了什么。测试不去敲真的 Ollama。"""
    with patch.object(local_defaults, "installed_ollama_models",
                      return_value=["llava:latest", "qwen3:14b"]) as listed:
        yield listed


class TestWhatIsMissingIsFilledWithTheLocalServices:
    def test_an_empty_config_gets_the_three_local_values(self, ollama):
        assert local_defaults.missing({}) == {
            **LOCAL_IMAGE, "ollama_model_name": "qwen3:14b",
        }

    def test_empty_strings_count_as_missing(self, ollama):
        """config.toml 里常见的是 ``key = ""``，不是没有这一行。"""
        fills = local_defaults.missing({
            "openai_image_base_url": "  ", "openai_image_model": "",
            "ollama_model_name": "", "llm_provider": "",
        })
        assert fills == {**LOCAL_IMAGE, "ollama_model_name": "qwen3:14b"}

    def test_the_recommended_model_is_preferred_when_it_is_installed(self, ollama):
        assert local_defaults.missing(dict(LOCAL_IMAGE))["ollama_model_name"] == (
            local_defaults.PREFERRED_OLLAMA_MODEL
        )

    def test_otherwise_the_first_installed_model_is_used(self, ollama):
        ollama.return_value = ["gemma4:26b", "llava:latest"]
        assert local_defaults.missing(dict(LOCAL_IMAGE)) == {
            "ollama_model_name": "gemma4:26b",
        }


class TestWhatIsAlreadySetIsNeverTouched:
    def test_a_complete_config_is_left_alone(self, ollama):
        app = {**LOCAL_IMAGE, "llm_provider": "ollama", "ollama_model_name": "x"}
        assert local_defaults.missing(app) == {}
        ollama.assert_not_called()

    def test_an_image_server_somewhere_else_is_not_redirected(self, ollama):
        app = {"openai_image_base_url": "https://relay.example/v1",
               "openai_image_model": "flux", "ollama_model_name": "x"}
        assert local_defaults.missing(app) == {}

    def test_a_half_configured_remote_image_server_is_not_guessed_at(self, ollama):
        """地址指向别处而模型空着：那是没配完，不是缺默认值。补一个本机的
        模型名进去，请求就会带着一个对方不认识的模型发到一个计费的地方。"""
        app = {"openai_image_base_url": "https://relay.example/v1",
               "ollama_model_name": "x"}
        assert local_defaults.missing(app) == {}

    def test_a_model_without_an_address_is_not_given_the_local_address(self, ollama):
        app = {"openai_image_model": "flux", "ollama_model_name": "x"}
        assert local_defaults.missing(app) == {}

    def test_the_local_address_alone_gets_its_own_model(self, ollama):
        app = {"openai_image_base_url": const.DEFAULT_OPENAI_IMAGE_BASE_URL,
               "ollama_model_name": "x"}
        assert local_defaults.missing(app) == {
            "openai_image_model": const.DEFAULT_OPENAI_IMAGE_MODEL,
        }

    def test_another_language_model_provider_is_not_given_an_ollama_model(
        self, ollama
    ):
        app = {**LOCAL_IMAGE, "llm_provider": "anthropic"}
        assert local_defaults.missing(app) == {}
        ollama.assert_not_called()

    def test_the_provider_itself_is_never_switched(self, ollama):
        for app in ({}, {"llm_provider": "anthropic"}, {"llm_provider": "ollama"}):
            assert "llm_provider" not in local_defaults.missing(app)
            assert "video_source" not in local_defaults.missing(app)


class TestWhenOllamaDoesNotAnswer:
    def test_no_model_is_made_up(self, ollama):
        ollama.return_value = []
        assert local_defaults.missing(dict(LOCAL_IMAGE)) == {}

    def test_the_form_is_told_what_is_still_missing(self):
        said = local_defaults.problems(dict(LOCAL_IMAGE))
        assert len(said) == 1 and "Ollama" in said[0] and "Settings" in said[0]

    def test_nothing_is_reported_once_everything_is_set(self):
        assert local_defaults.problems(
            {**LOCAL_IMAGE, "ollama_model_name": "qwen3:14b"}
        ) == []

    def test_a_connection_error_is_an_empty_list_not_a_crash(self):
        with patch.object(local_defaults.requests, "get",
                          side_effect=ConnectionError("refused")):
            assert local_defaults.installed_ollama_models({}) == []

    def test_embedding_models_are_not_offered_to_write_scripts(self):
        class Reply:
            def raise_for_status(self):
                return None

            def json(self):
                return {"models": [{"name": "nomic-embed-text:latest"},
                                   {"name": "qwen3:14b"}, {"name": ""}, "junk"]}

        with patch.object(local_defaults.requests, "get",
                          return_value=Reply()) as get:
            assert local_defaults.installed_ollama_models(
                {"ollama_base_url": "http://box:11434/v1/"}
            ) == ["qwen3:14b"]
        # 问的是 Ollama 自己的接口，不是 /v1 下面那个。
        assert get.call_args.args[0] == "http://box:11434/api/tags"


class TestWriting:
    def test_what_is_filled_is_saved(self, ollama):
        app = {}
        with patch.object(config, "app", app), \
                patch.object(config, "update_config_values_nonblocking",
                             side_effect=lambda section, changes:
                             section.update(changes) or True) as write, \
                patch.object(config, "try_save_config") as save:
            filled = local_defaults.apply()
        assert filled == {**LOCAL_IMAGE, "ollama_model_name": "qwen3:14b"}
        assert app == filled
        write.assert_called_once()
        save.assert_called_once()

    def test_nothing_missing_means_the_file_is_not_touched(self, ollama):
        app = {**LOCAL_IMAGE, "ollama_model_name": "x"}
        with patch.object(config, "app", app), \
                patch.object(config, "update_config_values_nonblocking") as write, \
                patch.object(config, "try_save_config") as save:
            assert local_defaults.apply() == {}
        write.assert_not_called()
        save.assert_not_called()

    def test_it_only_ever_writes_these_three_settings(self, ollama):
        """这是一个会自己改 config.toml 的函数。它能写的键必须数得过来。"""
        for app in ({}, {"llm_provider": "ollama"}, {"video_source": "pexels"}):
            assert set(local_defaults.missing(app)) <= {
                "openai_image_base_url", "openai_image_model", "ollama_model_name",
            }

    def test_importing_the_api_does_not_fill_anything(self):
        """只在 API 真正启动时补。导入不算：测试导入 asgi 不该去敲 Ollama、
        不该改配置。"""
        import inspect

        from app import asgi

        source = inspect.getsource(asgi)
        assert source.count("local_defaults.apply()") == 1
        assert "local_defaults.apply()" in inspect.getsource(asgi.application_lifespan)
