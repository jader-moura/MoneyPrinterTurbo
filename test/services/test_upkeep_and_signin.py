"""备份和维护、账号登录——从 WebUI 的设置对话框搬进编辑器的最后两块。

钉住的事：

* 密钥备份是"Key 不回传"的唯一例外，而且只在明说确认之后；
* 恢复密钥不能变成一个能改任何配置的后门；
* 备份和预设文件与 WebUI 的格式互通；
* 清缓存要确认，而且只清说好的那个范围；
* 三家账号登录在界面上是同一个形状，失败照实说。

Key backup is the one confirmed exception to "keys are never sent back";
restoring keys cannot become a way to write arbitrary settings; the files are
interchangeable with the WebUI's; a cache clean-up must be confirmed; and the
three account sign-ins present one shape, reporting failures as they are.
"""

import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.config import config
from app.services import signin, upkeep

ROOT = Path(__file__).resolve().parents[2]
SECRET = "sk-BACKUP-ONLY"
SECTIONS = ("app", "ui", "azure", "chatterbox", "elevenlabs",
            "minimax_tts", "siliconflow", "fish_audio", "voxcpm")


@pytest.fixture
def saved():
    sections = {name: {} for name in SECTIONS}
    sections["app"].update({
        "openai_api_key": SECRET, "openai_model_name": "gpt-4o",
        "pexels_api_keys": [SECRET, ""], "endpoint": "http://here",
        "upload_post_username": "me", "video_source": "pexels",
    })
    sections["azure"].update({"speech_key": SECRET, "speech_region": "eastus"})
    sections["ui"].update({"voice_name": "chatterbox:Adam-Male", "font_size": 75})

    def write(section, changes):
        section.update(changes)
        return True

    started = [patch.object(config, name, sections[name]) for name in SECTIONS]
    started += [
        patch.object(config, "update_config_values_nonblocking", side_effect=write),
        patch.object(config, "snapshot_config_with_pending",
                     side_effect=lambda section: dict(section)),
    ]
    for one in started:
        one.start()
    save = patch.object(config, "try_save_config", return_value=True).start()
    try:
        yield {"sections": sections, "save": save}
    finally:
        patch.stopall()


class TestKeyBackup:
    def test_it_holds_the_keys_and_what_they_need_to_work(self, saved):
        keys = upkeep.key_backup()["keys"]
        assert keys["app"]["openai_api_key"] == SECRET
        assert keys["app"]["pexels_api_keys"] == [SECRET]
        # 只有 Key 没有区域，Azure 那把 Key 换台机器还是用不了。
        assert keys["azure"] == {"speech_key": SECRET, "speech_region": "eastus"}
        assert keys["app"]["upload_post_username"] == "me"

    def test_it_holds_nothing_else(self, saved):
        keys = upkeep.key_backup()["keys"]
        assert "openai_model_name" not in keys["app"]
        assert "endpoint" not in keys["app"]
        assert "ui" not in keys

    def test_a_restore_puts_the_keys_back(self, saved):
        raw = json.dumps(upkeep.key_backup()).encode()
        for section in saved["sections"].values():
            section.clear()
        result = upkeep.restore_keys(raw)
        assert result["restored"] == 5
        assert saved["sections"]["app"]["openai_api_key"] == SECRET
        assert saved["sections"]["azure"]["speech_region"] == "eastus"
        saved["save"].assert_called()

    def test_a_restore_cannot_write_settings_that_are_not_keys(self, saved):
        """备份文件可以手工改。它不能变成一个改任何配置的后门。"""
        raw = json.dumps({
            "schema": upkeep.KEY_BACKUP_SCHEMA, "version": upkeep.KEY_BACKUP_VERSION,
            "keys": {
                "app": {"openai_api_key": "sk-new", "endpoint": "http://evil",
                        "openai_base_url": "http://evil", "listen_port": 1},
                "ui": {"voice_name": "x", "some_api_key": "x"},
                "nope": {"api_key": "x"},
            },
        }).encode()
        assert upkeep.restore_keys(raw)["restored"] == 1
        app = saved["sections"]["app"]
        assert app["openai_api_key"] == "sk-new"
        assert app["endpoint"] == "http://here"
        assert "openai_base_url" not in app and "listen_port" not in app
        assert saved["sections"]["ui"] == {"voice_name": "chatterbox:Adam-Male",
                                           "font_size": 75}

    @pytest.mark.parametrize("raw, said", [
        (b"not json", "not JSON"),
        (b'{"schema": "something.else", "version": 1}', "not a key backup"),
        (json.dumps({"schema": upkeep.KEY_BACKUP_SCHEMA, "version": 99}).encode(),
         "version 99"),
        (json.dumps({"schema": upkeep.KEY_BACKUP_SCHEMA, "version": 1,
                     "keys": {"app": {"endpoint": "x"}}}).encode(), "no keys"),
        (b" " * (upkeep.MAX_TRANSFER_BYTES + 1), "larger than 2 MB"),
    ], ids=["not-json", "other-schema", "newer-version", "no-keys", "too-large"])
    def test_a_file_that_is_not_a_backup_is_refused_untouched(self, saved, raw, said):
        before = json.dumps(saved["sections"], sort_keys=True)
        with pytest.raises(upkeep.UpkeepError, match=said):
            upkeep.restore_keys(raw)
        assert json.dumps(saved["sections"], sort_keys=True) == before

    def test_a_settings_preset_is_not_accepted_as_a_key_backup(self, saved):
        raw = json.dumps(upkeep.settings_preset()).encode()
        with pytest.raises(upkeep.UpkeepError, match="not a key backup"):
            upkeep.restore_keys(raw)

    def test_the_files_are_the_same_format_as_the_old_pages(self):
        """两边导出的文件要能互相导入。"""
        source = (ROOT / "webui" / "Main.py").read_text(encoding="utf-8")
        for name in ("SETTINGS_PRESET_SCHEMA", "SETTINGS_PRESET_VERSION",
                     "KEY_BACKUP_SCHEMA", "KEY_BACKUP_VERSION",
                     "KEY_BACKUP_FILE_NAME", "SETTINGS_PRESET_FILE_NAME"):
            old = re.search(rf"^{name} = (.+)$", source, re.M).group(1)
            assert json.loads(old) == getattr(upkeep, name), name
        suffixes = re.search(r"^CREDENTIAL_KEY_SUFFIXES = \((.*?)^\)", source,
                             re.S | re.M).group(1)
        assert tuple(re.findall(r'"(\w+)"', suffixes)) == (
            upkeep.CREDENTIAL_KEY_SUFFIXES
        )


class TestSettingsPreset:
    def test_it_carries_the_last_used_choices_and_no_keys(self, saved):
        preset = upkeep.settings_preset()
        assert preset["params"]["voice_name"] == "chatterbox:Adam-Male"
        assert preset["params"]["font_size"] == 75
        assert preset["params"]["video_source"] == "pexels"
        assert SECRET not in json.dumps(preset)

    def test_it_leaves_out_what_only_exists_on_this_computer(self, saved):
        params = upkeep.settings_preset()["params"]
        assert "video_materials" not in params
        assert "custom_audio_file" not in params

    def test_loading_one_makes_it_what_the_next_video_starts_from(self, saved):
        preset = upkeep.settings_preset()
        preset["params"].update(voice_name="en-US-AvaNeural-Female", font_size=90,
                                video_source="pixabay")
        assert upkeep.apply_preset(json.dumps(preset).encode()) == {"applied": True}
        assert saved["sections"]["ui"]["voice_name"] == "en-US-AvaNeural-Female"
        assert saved["sections"]["ui"]["font_size"] == 90
        assert saved["sections"]["app"]["video_source"] == "pixabay"

    def test_a_preset_with_a_value_out_of_range_is_refused_whole(self, saved):
        preset = upkeep.settings_preset()
        preset["params"]["font_size"] = "huge"
        with pytest.raises(upkeep.UpkeepError, match="font_size"):
            upkeep.apply_preset(json.dumps(preset).encode())
        assert saved["sections"]["ui"]["font_size"] == 75

    def test_a_key_backup_is_not_accepted_as_a_preset(self, saved):
        with pytest.raises(upkeep.UpkeepError, match="not a settings preset"):
            upkeep.apply_preset(json.dumps(upkeep.key_backup()).encode())


class TestCacheCleanUp:
    @pytest.fixture
    def cache(self):
        stats = SimpleNamespace(file_count=10, total_size=1000, oldest_mtime=1.0,
                                newest_mtime=2.0)
        old = SimpleNamespace(file_count=4, total_size=400, oldest_mtime=1.0,
                              newest_mtime=1.5)
        done = SimpleNamespace(deleted_count=4, deleted_size=400, failed_count=0)
        with patch.object(upkeep.cache_manager, "get_video_cache_stats",
                          side_effect=lambda max_age_days=None:
                          stats if max_age_days is None else old), \
                patch.object(upkeep.cache_manager, "clean_video_cache",
                             return_value=done) as clean, \
                patch.object(upkeep.cache_manager, "video_cache_dir",
                             return_value="C:/cache"):
            yield clean

    def test_looking_deletes_nothing(self, cache):
        data = upkeep.cache_stats(30)
        assert (data["files"], data["would_delete"]["files"]) == (10, 4)
        cache.assert_not_called()

    def test_it_does_not_delete_without_being_told_twice(self, cache):
        for confirm in (False, None, "yes", 1):
            with pytest.raises(upkeep.UpkeepError, match="confirm"):
                upkeep.clean_cache(30, confirm)
        cache.assert_not_called()

    def test_it_deletes_exactly_the_range_that_was_shown(self, cache):
        data = upkeep.clean_cache(30, True)
        cache.assert_called_once_with(max_age_days=30)
        assert data["deleted"] == {"files": 4, "bytes": 400, "failed": 0}

    @pytest.mark.parametrize("days", [0, 1, -5, 365, "30"])
    def test_a_range_that_was_never_offered_is_refused(self, cache, days):
        with pytest.raises(upkeep.UpkeepError):
            upkeep.clean_cache(days, True)
        cache.assert_not_called()


class TestLogging:
    def test_an_unknown_level_is_refused(self):
        with patch.object(config, "update_logging_options") as update:
            with pytest.raises(upkeep.UpkeepError, match="(?i)loud"):
                upkeep.set_logging("loud", False)
            with pytest.raises(upkeep.UpkeepError):
                upkeep.set_logging("INFO", "yes")
        update.assert_not_called()

    def test_a_level_is_applied(self):
        with patch.object(config, "update_logging_options") as update, \
                patch.object(config, "try_save_config"):
            upkeep.set_logging("warning", True)
        update.assert_called_once_with("WARNING", True)


@pytest.fixture
def fresh_signin():
    signin._flows.clear()
    signin._notes.clear()
    yield
    signin._flows.clear()
    signin._notes.clear()


class TestSignIn:
    def test_only_the_three_account_providers_sign_in(self, fresh_signin):
        from app.services import app_settings

        assert set(signin.PROVIDERS) == set(app_settings.SIGN_IN_PROVIDERS)
        with pytest.raises(signin.SignInError, match="openai"):
            signin.status("openai")

    def test_the_settings_show_a_sign_in_where_there_is_no_key(self):
        from app.services import app_settings

        llm = next(g for g in app_settings.groups() if g["id"] == "llm")
        panels = {panel["id"]: panel for panel in llm["panels"]}
        for provider in signin.PROVIDERS:
            assert panels[provider]["signin"] == provider
            assert not any(app_settings._is_secret(f)
                           for f in panels[provider]["fields"])
        assert "signin" not in panels["openai"]

    def test_codex_shows_a_code_then_connects(self, fresh_signin):
        flow = {"user_code": "ABCD-1234", "interval": 0, "generation": 7,
                "expires_at": 9e12}
        with patch.object(signin.codex_oauth, "request_device_code",
                          return_value=flow), \
                patch.object(signin.codex_oauth, "authorization_generation",
                             return_value=7), \
                patch.object(signin.codex_oauth, "account_status",
                             return_value=False), \
                patch.object(signin.codex_oauth, "poll_login",
                             side_effect=[False, True]) as poll:
            started = signin.begin("openai_codex")
            assert started["pending"]["code"] == "ABCD-1234"
            assert started["pending"]["url"] == signin.codex_oauth.VERIFY_URL
            assert "pending" in signin.poll("openai_codex")
            done = signin.poll("openai_codex")
        assert "pending" not in done
        assert done["note"]["kind"] == "ok"
        assert poll.call_count == 2

    def test_it_does_not_ask_the_other_side_faster_than_it_allows(
        self, fresh_signin
    ):
        flow = {"user_code": "X", "interval": 600, "generation": 1,
                "expires_at": 9e12}
        with patch.object(signin.codex_oauth, "request_device_code",
                          return_value=flow), \
                patch.object(signin.codex_oauth, "account_status",
                             return_value=False), \
                patch.object(signin.codex_oauth, "poll_login") as poll:
            signin.begin("openai_codex")
            for _ in range(5):
                signin.poll("openai_codex")
        poll.assert_not_called()

    def test_a_code_that_expired_says_so_and_stops(self, fresh_signin):
        flow = {"user_code": "X", "interval": 0, "generation": 1, "expires_at": 1}
        with patch.object(signin.codex_oauth, "request_device_code",
                          return_value=flow), \
                patch.object(signin.codex_oauth, "authorization_generation",
                             return_value=1), \
                patch.object(signin.codex_oauth, "account_status",
                             return_value=False):
            signin.begin("openai_codex")
            done = signin.poll("openai_codex")
        assert "pending" not in done
        assert "expired" in done["note"]["message"].lower()

    def test_credentials_that_cannot_be_read_can_still_be_cleared(
        self, fresh_signin
    ):
        broken = signin.codex_oauth.CodexError("codex.oauth.storage_error")
        with patch.object(signin.codex_oauth, "account_status", side_effect=broken):
            state = signin.status("openai_codex")
        assert state["connected"] is False and state["broken"] is True
        assert "credentials" in state["problem"].lower()

    def test_chatgpt_signed_in_without_plan_access_is_not_called_connected(
        self, fresh_signin
    ):
        details = {"connected": True, "authorized": False, "email": "a@b.c",
                   "profiles": {}, "active": "", "revision": 0}
        with patch.object(signin.chatgpt_oauth, "account_details",
                          return_value=details):
            state = signin.status("openai_chatgpt")
        assert state["connected"] is False
        assert state["signed_in"] is True and state["account"] == "a@b.c"
        assert "plan" in state["problem"].lower()

    def test_chatgpt_disconnect_that_was_not_confirmed_remotely_says_so(
        self, fresh_signin
    ):
        details = {"connected": False, "authorized": False, "email": ""}
        with patch.object(signin.chatgpt_oauth, "disconnect", return_value=False), \
                patch.object(signin.chatgpt_oauth, "account_details",
                             return_value=details):
            state = signin.disconnect("openai_chatgpt")
        assert state["note"]["kind"] == "warn"

    def test_kimi_survives_a_network_blip_but_not_five(self, fresh_signin):
        import requests

        raw = {"device_code": "d", "user_code": "u", "interval": 0,
               "verification_uri_complete": "https://kimi/x", "expires_in": 600}
        with patch.object(signin.kimi_code_oauth, "request_device_code",
                          return_value=raw), \
                patch.object(signin.kimi_code_oauth, "resolve_region",
                             return_value="china"), \
                patch.object(signin.kimi_code_oauth, "authorization_generation",
                             return_value=1), \
                patch.object(signin.kimi_code_oauth, "is_authorized",
                             return_value=False), \
                patch.object(signin.kimi_code_oauth, "poll_token",
                             side_effect=requests.ConnectionError("down")), \
                patch.object(signin, "_flows", signin._flows):
            signin.begin("kimi_code")
            # 间隔被服务端的下限抬到 2 秒；测试里直接把"下次能问的时间"拨回去。
            for _ in range(4):
                signin._flows["kimi_code"]["next_poll_at"] = 0
                assert "pending" in signin.poll("kimi_code")
            signin._flows["kimi_code"]["next_poll_at"] = 0
            done = signin.poll("kimi_code")
        assert "pending" not in done and done["note"]["kind"] == "warn"

    def test_kimi_declined_is_reported_as_declined(self, fresh_signin):
        raw = {"device_code": "d", "user_code": "u", "interval": 0,
               "verification_uri_complete": "https://kimi/x", "expires_in": 600}
        with patch.object(signin.kimi_code_oauth, "request_device_code",
                          return_value=raw), \
                patch.object(signin.kimi_code_oauth, "resolve_region",
                             return_value="china"), \
                patch.object(signin.kimi_code_oauth, "authorization_generation",
                             return_value=1), \
                patch.object(signin.kimi_code_oauth, "is_authorized",
                             return_value=False), \
                patch.object(signin.kimi_code_oauth, "poll_token",
                             return_value={"denied": True}), \
                patch.object(signin.kimi_code_oauth, "store_token_bundle") as store:
            signin.begin("kimi_code")
            signin._flows["kimi_code"]["next_poll_at"] = 0
            done = signin.poll("kimi_code")
        assert "declined" in done["note"]["message"].lower()
        store.assert_not_called()

    def test_cancelling_leaves_a_connected_account_alone(self, fresh_signin):
        with patch.object(signin.kimi_code_oauth, "is_authorized",
                          return_value=True), \
                patch.object(signin.kimi_code_oauth, "authorized_region",
                             return_value="china"), \
                patch.object(signin.kimi_code_oauth, "resolve_region",
                             return_value="china"), \
                patch.object(signin.kimi_code_oauth, "clear_credentials") as clear:
            state = signin.cancel("kimi_code")
        assert state["connected"] is True
        clear.assert_not_called()


class TestOverHttp:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from app import asgi

        return TestClient(asgi.app)

    def test_the_keys_are_not_handed_over_by_a_plain_link(self, client, saved):
        """GET 会被预取、会留在历史记录里。备份只能是一次明说确认的 POST。"""
        assert client.get("/api/v1/settings/keys/export").status_code in (404, 405)
        refused = client.post("/api/v1/settings/keys/export", json={})
        assert refused.status_code == 400 and SECRET not in refused.text
        refused = client.post("/api/v1/settings/keys/export",
                              json={"confirm": False})
        assert refused.status_code == 400 and SECRET not in refused.text

    def test_a_confirmed_backup_and_a_restore_round_trip(self, client, saved):
        got = client.post("/api/v1/settings/keys/export", json={"confirm": True})
        assert got.status_code == 200, got.text
        data = got.json()["data"]
        assert data["filename"] == upkeep.KEY_BACKUP_FILE_NAME
        saved["sections"]["app"]["openai_api_key"] = ""
        back = client.post("/api/v1/settings/keys/restore", files={
            "file": ("keys.json", json.dumps(data["payload"]), "application/json"),
        })
        assert back.status_code == 200, back.text
        assert saved["sections"]["app"]["openai_api_key"] == SECRET
        # 恢复的回执里不带 Key。
        assert SECRET not in back.text

    def test_a_bad_file_is_a_400_with_the_reason(self, client, saved):
        got = client.post("/api/v1/settings/keys/restore",
                          files={"file": ("x.json", b"nope", "application/json")})
        assert got.status_code == 400
        assert "not JSON" in got.json()["message"]

    def test_cleaning_the_cache_without_confirming_is_refused(self, client):
        with patch.object(upkeep.cache_manager, "clean_video_cache") as clean:
            got = client.post("/api/v1/settings/cache/clean", json={"days": 30})
        assert got.status_code == 400
        clean.assert_not_called()

    def test_an_unknown_sign_in_step_or_provider_is_refused(self, client):
        assert client.post(
            "/api/v1/settings/signin/openai_codex/explode").status_code == 404
        assert client.get("/api/v1/settings/signin/openai").status_code == 400


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

    def test_the_backup_and_the_clean_up_ask_before_acting(self):
        body = self._body(self._script(), "upkeepBox")
        export = body[:body.index('"/settings/keys/export"')]
        assert "window.confirm(" in export[export.rindex("press("):]
        clean = body[:body.index('"/settings/cache/clean"')]
        assert "window.confirm(" in clean[clean.rindex("press("):]

    def test_the_delete_button_says_how_much_and_is_off_when_there_is_nothing(self):
        body = self._body(self._script(), "upkeepBox")
        assert "would.files" in body
        assert "clean.disabled = upkeep.busy || !would.files;" in body

    def test_the_tools_group_has_no_save_button(self):
        body = self._body(self._script(), "drawPrefs")
        assert "ui.settingsSave.hidden = !!group.tools;" in body

    def test_sign_in_is_only_polled_while_it_is_pending_and_on_screen(self):
        body = self._body(self._script(), "watchSignins")
        assert "one.pending" in body
        assert "ui.settingsModal.hidden" in body
        assert "clearTimeout(signins.timer);" in body

    def test_the_editor_script_defines_no_function_twice(self):
        """后写的那个会悄悄顶掉先写的。设置窗最初就叫 ``drawSettings``，把
        时间线上显示渲染参数的那个同名函数顶掉了——语法完全合法，
        ``node --check`` 一声不吭。"""
        import collections

        names = re.findall(r"^(?:async )?function (\w+)\(", self._script(), re.M)
        twice = [name for name, count in collections.Counter(names).items()
                 if count > 1]
        assert twice == []

    def test_a_sign_in_panel_is_drawn_where_the_server_says(self):
        body = self._body(self._script(), "drawPrefs")
        assert "if (panel.signin) box.appendChild(signinBox(panel.signin));" in body
