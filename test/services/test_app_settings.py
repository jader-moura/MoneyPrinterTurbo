"""编辑器里的"设置"。

这是一个能读写 config.toml 的 HTTP 接口，所以钉得最紧的是三件事：

* Key 不回传——读出来只有"设过没有"；
* 只有表里列了的键能写——表外的键直接拒绝；
* 凭据留空等于不动——那个框本来就是空的，把"没填"当"清空"会让用户每保存
  一次别的东西就丢一把 Key。

The settings window is an HTTP surface that reads and writes config.toml, so
the three things pinned hardest are: secrets are never sent back, only listed
keys can be written, and an empty secret box means "keep it".
"""

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import config
from app.services import app_settings

ROOT = Path(__file__).resolve().parents[2]
SECRET = "sk-THIS-MUST-NEVER-LEAVE-THE-SERVER"

SECTION_NAMES = ("app", "ui", "azure", "chatterbox", "kokoro", "elevenlabs",
                 "minimax_tts", "siliconflow", "fish_audio", "voxcpm")


@pytest.fixture
def settings():
    """一份假的配置，以及"写了什么"的记录。不碰真的 config.toml。"""
    sections = {name: {} for name in SECTION_NAMES}
    sections["app"].update({
        "llm_provider": "openai",
        "openai_api_key": SECRET,
        "openai_model_name": "gpt-4o",
        "pexels_api_keys": [SECRET, SECRET + "-2"],
        "material_concurrency": 4,
    })
    sections["kokoro"].update({"base_url": "http://127.0.0.1:8880/v1"})
    sections["azure"].update({"speech_key": SECRET, "speech_region": "eastus"})
    state = {"busy": False}

    def write(section, changes):
        if state["busy"]:
            return False
        section.update(changes)
        return True

    patches = [patch.object(config, name, sections[name]) for name in SECTION_NAMES]
    patches += [
        patch.object(config, "update_config_values_nonblocking", side_effect=write),
        patch.object(config, "snapshot_config_with_pending",
                     side_effect=lambda section: dict(section)),
    ]
    for one in patches:
        one.start()
    save = patch.object(config, "try_save_config",
                        side_effect=lambda: not state["busy"]).start()
    try:
        yield {"sections": sections, "save": save, "state": state}
    finally:
        patch.stopall()


class TestKeysNeverLeaveTheServer:
    def test_reading_the_settings_does_not_carry_a_single_key(self, settings):
        assert SECRET not in json.dumps(app_settings.describe())

    def test_a_key_reads_back_as_set_or_not_set(self, settings):
        values = app_settings.describe()["values"]
        assert values["app.openai_api_key"] == {"set": True, "count": 1}
        assert values["app.pexels_api_keys"] == {"set": True, "count": 2}
        assert values["app.pixabay_api_keys"] == {"set": False, "count": 0}
        assert values["azure.speech_key"] == {"set": True, "count": 1}

    def test_saving_does_not_echo_the_key_back_either(self, settings):
        result = app_settings.apply({"app.deepseek_api_key": SECRET + "-new"})
        assert SECRET not in json.dumps(result)
        assert settings["sections"]["app"]["deepseek_api_key"] == SECRET + "-new"

    def test_everything_that_looks_like_a_credential_is_treated_as_one(self):
        """表里新加一项时最容易犯的错：一个 Key 被标成了普通文本。"""
        for row in app_settings._all_fields().values():
            name = row["key"]
            looks_secret = name.endswith(("_api_key", "_api_keys", "_token")) or (
                name in ("api_key", "speech_key")
            )
            if looks_secret:
                assert app_settings._is_secret(row), row["id"]

    def test_the_plain_values_are_still_readable(self, settings):
        values = app_settings.describe()["values"]
        assert values["app.llm_provider"] == "openai"
        assert values["app.openai_model_name"] == "gpt-4o"
        assert values["kokoro.base_url"] == "http://127.0.0.1:8880/v1"
        assert values["azure.speech_region"] == "eastus"


class TestOnlyListedSettingsCanBeWritten:
    @pytest.mark.parametrize("field_id", [
        "app.api_key",               # 接口自己的访问口令：不在表里
        "app.endpoint",
        "ui.language",
        "nope.thing",
        "app",
        "",
    ])
    def test_a_key_outside_the_table_is_refused(self, settings, field_id):
        before = json.dumps(settings["sections"], sort_keys=True)
        with pytest.raises(app_settings.SettingsError, match="unknown setting"):
            app_settings.apply({field_id: "x"})
        assert json.dumps(settings["sections"], sort_keys=True) == before
        settings["save"].assert_not_called()

    def test_one_bad_change_stops_the_whole_save(self, settings):
        """要么都存，要么都不存：存了一半的设置比没存更难查。"""
        with pytest.raises(app_settings.SettingsError):
            app_settings.apply({
                "app.openai_model_name": "gpt-5", "app.not_a_setting": "x",
            })
        assert settings["sections"]["app"]["openai_model_name"] == "gpt-4o"

    def test_every_field_writes_to_a_real_config_section(self):
        for row in app_settings._all_fields().values():
            assert row["section"] in app_settings._sections(), row["id"]

    def test_a_setting_appears_once_unless_two_services_share_the_key(self):
        """Gemini 和 MiMo 既是语言模型又是配音服务，用的是同一把 Key，所以
        在两处都出现——那是同一项，不是两项。别的重复就是抄错了。"""
        seen = []
        for group in app_settings.groups():
            rows = list(group["fields"])
            for panel in group["panels"]:
                rows += panel["fields"]
            seen += [row["id"] for row in rows]
        twice = {one for one in seen if seen.count(one) > 1}
        assert twice == {"app.gemini_api_key", "app.mimo_api_key"}


class TestSavingKeys:
    def test_an_empty_box_keeps_the_key(self, settings):
        result = app_settings.apply({
            "app.openai_api_key": "", "app.pexels_api_keys": "  ",
            "app.openai_model_name": "gpt-5",
        })
        app = settings["sections"]["app"]
        assert app["openai_api_key"] == SECRET
        assert app["pexels_api_keys"] == [SECRET, SECRET + "-2"]
        assert app["openai_model_name"] == "gpt-5"
        assert result["changed"] == ["app.openai_model_name"]

    def test_typing_a_key_replaces_it(self, settings):
        app_settings.apply({"app.openai_api_key": "sk-new"})
        assert settings["sections"]["app"]["openai_api_key"] == "sk-new"

    def test_several_stock_keys_are_split_like_the_old_page_did(self, settings):
        app_settings.apply({"app.pixabay_api_keys": "one, two ,three"})
        assert settings["sections"]["app"]["pixabay_api_keys"] == [
            "one", "two", "three",
        ]

    def test_removing_a_key_has_to_be_asked_for_by_name(self, settings):
        result = app_settings.apply({}, clear=["app.openai_api_key",
                                               "app.pexels_api_keys"])
        app = settings["sections"]["app"]
        assert app["openai_api_key"] == ""
        assert app["pexels_api_keys"] == []
        assert result["values"]["app.openai_api_key"] == {"set": False, "count": 0}

    def test_only_a_key_can_be_removed_that_way(self, settings):
        with pytest.raises(app_settings.SettingsError, match="only a key"):
            app_settings.apply({}, clear=["app.openai_model_name"])


class TestSavingTheRest:
    def test_a_choice_must_be_one_of_the_choices(self, settings):
        with pytest.raises(app_settings.SettingsError, match="not one of"):
            app_settings.apply({"app.llm_provider": "skynet"})
        app_settings.apply({"app.llm_provider": "ollama"})
        assert settings["sections"]["app"]["llm_provider"] == "ollama"

    def test_a_number_choice_is_saved_as_a_number(self, settings):
        """下拉框交上来的是字符串；存成 "4" 的话渲染那边拿它当并发数会出错。"""
        app_settings.apply({"app.material_concurrency": "8"})
        assert settings["sections"]["app"]["material_concurrency"] == 8

    def test_a_switch_must_be_on_or_off(self, settings):
        with pytest.raises(app_settings.SettingsError, match="on or off"):
            app_settings.apply({"app.upload_post_enabled": "yes"})
        app_settings.apply({"app.upload_post_enabled": True})
        assert settings["sections"]["app"]["upload_post_enabled"] is True

    def test_a_number_is_checked_against_its_range(self, settings):
        with pytest.raises(app_settings.SettingsError, match="between"):
            app_settings.apply({"app.codex_image_timeout": 5})
        app_settings.apply({"app.codex_image_timeout": "300"})
        assert settings["sections"]["app"]["codex_image_timeout"] == 300

    def test_a_switch_that_was_never_saved_shows_its_default(self, settings):
        values = app_settings.describe()["values"]
        assert values["ui.review_prompts_before_images"] is True
        assert values["app.upload_post_enabled"] is False

    def test_nothing_changed_means_nothing_saved(self, settings):
        result = app_settings.apply({})
        assert result["changed"] == []
        settings["save"].assert_not_called()

    def test_while_a_video_is_being_made_the_change_is_queued_and_says_so(
        self, settings
    ):
        """有片子在跑时配置是锁着的。保存不能卡住，但也不能说"已生效"。"""
        settings["state"]["busy"] = True
        result = app_settings.apply({"app.openai_model_name": "gpt-5"})
        assert result["deferred"] is True
        settings["state"]["busy"] = False
        assert app_settings.apply({"app.openai_model_name": "gpt-5"})[
            "deferred"] is False


class TestItCoversWhatTheOldPageOffered:
    def test_every_language_model_provider_has_a_panel(self):
        from app.models.llm_provider import LLM_PROVIDER_REGISTRY

        group = next(g for g in app_settings.groups() if g["id"] == "llm")
        assert [p["id"] for p in group["panels"]] == [
            provider.provider_id for provider in LLM_PROVIDER_REGISTRY
        ]
        choices = [one["id"] for one in group["fields"][0]["options"]]
        assert choices == [p["id"] for p in group["panels"]]
        # 界面靠这一项决定展开哪一家。
        assert group["panel_field"] == group["fields"][0]["id"]

    def test_every_provider_key_the_old_page_wrote_can_be_set_here(self):
        """WebUI 里写过的每一个凭据键。少一个，那一家就还得回 :8501 去配。"""
        source = (ROOT / "webui" / "Main.py").read_text(encoding="utf-8")
        wrote = set(re.findall(
            r'_set_runtime_config\(\s*"(\w+)",\s*"(\w*(?:api_key|speech_key|token))"',
            source,
        ))
        known = {(row["section"], row["key"])
                 for row in app_settings._all_fields().values()}
        assert wrote <= known, wrote - known

    def test_the_stock_sites_keys_are_here(self):
        known = {row["id"] for row in app_settings._all_fields().values()}
        for site in ("pexels", "pixabay", "coverr", "wavespeed"):
            assert f"app.{site}_api_keys" in known


class TestOverHttp:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from app import asgi

        return TestClient(asgi.app)

    def test_reading_over_http_carries_no_key(self, client, settings):
        got = client.get("/api/v1/settings")
        assert got.status_code == 200, got.text
        assert SECRET not in got.text
        assert got.json()["data"]["values"]["app.openai_api_key"]["set"] is True

    def test_saving_over_http(self, client, settings):
        got = client.put("/api/v1/settings", json={
            "changes": {"app.openai_model_name": "gpt-5", "app.openai_api_key": ""},
            "clear": ["azure.speech_key"],
        })
        assert got.status_code == 200, got.text
        assert SECRET not in got.text
        assert settings["sections"]["app"]["openai_model_name"] == "gpt-5"
        assert settings["sections"]["app"]["openai_api_key"] == SECRET
        assert settings["sections"]["azure"]["speech_key"] == ""

    def test_a_refusal_says_which_setting(self, client, settings):
        got = client.put("/api/v1/settings",
                         json={"changes": {"app.endpoint": "http://evil"}})
        assert got.status_code == 400
        assert "app.endpoint" in got.json()["message"]


class TestTheEditorOffersIt:
    @staticmethod
    def _script() -> str:
        return (ROOT / "resource" / "public" / "editor" / "editor.js").read_text(
            encoding="utf-8"
        )

    @staticmethod
    def _body(source: str, name: str) -> str:
        start = source.index(f"function {name}(")
        ends = [at for at in (source.find("\nfunction ", start + 1),
                              source.find("\nasync function ", start + 1)) if at > 0]
        return source[start:min(ends)]

    def test_a_key_box_never_shows_a_stored_value(self):
        body = self._body(self._script(), "settingField")
        assert 'input.type = "password";' in body
        # 框里只放用户这一次敲的东西，不放读回来的值。
        assert 'input.value = prefs.changes[field.id] || "";' in body

    def test_an_emptied_key_box_is_not_sent_as_a_change(self):
        body = self._body(self._script(), "settingField")
        typed = body[body.index('input.addEventListener("input"'):]
        assert "else delete prefs.changes[field.id];" in typed[:typed.index("});")]

    def test_saving_sends_only_what_was_changed(self):
        body = self._body(self._script(), "saveSettings")
        assert 'call("PUT", "/settings", { changes, clear })' in body
        assert "const changes = { ...prefs.changes };" in body

    def test_after_saving_the_new_video_form_is_read_again(self):
        """换了配音服务的地址，表单里那份音色清单就过期了。"""
        body = self._body(self._script(), "saveSettings")
        assert "fresh.form = null;" in body

    def test_it_says_so_when_the_change_is_only_queued(self):
        body = self._body(self._script(), "saveSettings")
        assert "data.deferred" in body
