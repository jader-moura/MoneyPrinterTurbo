"""在编辑器里开一条新片子。

以前一条片子只能从 WebUI 开始。这里钉住的是搬进编辑器之后必须成立的几件事：

* 表单的默认值就是上次用的设置，点了创建之后这次的选择也照 WebUI 的做法
  存回去——两个入口看到的是同一份；
* WebUI "点生成之前"能设的每一类东西这里都能设；
* 表单没列的来源点名拒绝，不悄悄换一家；按次计费的来源要勾了确认才开；
* 从这里开的片子永远停下来等人看，不会一路跑到成片。

Starting a video from the editor: defaults are the last-used settings and are
remembered again on create, every kind of option the WebUI offered is here, a
source the form does not offer is refused by name, a per-video charge has to
be confirmed, and the run always stops for review.
"""

import io
import os
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from app.config import config
from app.models import image_style as image_style_model
from app.models.schema import NewVideoRequest, TaskVideoRequest
from app.services import new_video

ROOT = Path(__file__).resolve().parents[2]

SAVED_UI = {
    "video_language": "en-US",
    "voice_name": "kokoro:am_adam",
    "voice_rate": 0.8,
    "video_clip_duration": 4,
    "font_name": "MicrosoftYaHeiBold.ttc",
    "text_fore_color": "#FFD700",
    "subtitle_enabled": True,
    "bgm_type": "random",
    "bgm_volume": 0.1,
    "video_aspect_openai_image": "16:9",
    "review_prompts_before_images": True,
}
SAVED_APP = {"video_source": "openai_image"}


@pytest.fixture
def saved(tmp_path):
    """一份存下来的设置，外加一个不碰真盘、不碰真服务的环境。"""
    storage = tmp_path / "storage"

    def storage_dir(sub="", create=False):
        path = storage / sub
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return str(path)

    written = {"ui": {}, "app": {}}

    def write(section, changes):
        written["ui" if section is config.ui else "app"].update(changes)
        section.update(changes)
        return True

    with patch.object(config, "ui", dict(SAVED_UI)), \
            patch.object(config, "app", dict(SAVED_APP)), \
            patch.object(config, "update_config_values_nonblocking",
                         side_effect=write), \
            patch.object(config, "try_save_config", return_value=True) as save, \
            patch.object(new_video.voice, "get_kokoro_voices",
                         return_value=["kokoro:af_heart", "kokoro:am_adam"]), \
            patch.object(new_video.voice, "get_all_azure_voices",
                         return_value=["en-US-AvaNeural-Female",
                                       "zh-CN-YunxiNeural-Male"]), \
            patch.object(new_video.local_defaults, "apply", return_value={}),             patch.object(new_video.local_defaults, "problems", return_value=[]),             patch.object(new_video, "_tracks", return_value=["calm.mp3"]), \
            patch.object(new_video, "_builtin_tracks", return_value=["calm.mp3"]), \
            patch.object(new_video, "_sounds", return_value=["whoosh.mp3"]), \
            patch.object(new_video, "_fonts",
                         return_value=["MicrosoftYaHeiBold.ttc"]), \
            patch.object(new_video.bgm, "resolve_bgm_file",
                         side_effect=_resolve_track), \
            patch.object(new_video.utils, "storage_dir", side_effect=storage_dir):
        yield {"save": save, "written": written, "storage": storage}


def _resolve_track(name):
    if name not in ("calm.mp3", "mine.mp3"):
        raise ValueError("file does not exist")
    return os.path.join("songs", name)


def _fields(form):
    return {field["name"]: field for tab in form["tabs"] for field in tab["fields"]}


class TestTheFormStartsFromWhatTheUserLastUsed:
    def test_the_saved_settings_are_the_defaults(self, saved):
        values = new_video.form()["values"]
        assert values["voice_name"] == "kokoro:am_adam"
        assert values["voice_rate"] == 0.8
        assert values["video_clip_duration"] == 4
        assert values["video_language"] == "en-US"
        assert values["video_source"] == "openai_image"
        assert values["music_type"] == "random"
        assert values["tts_server"] == "kokoro"
        assert values["voice_mode"] == "tts"

    def test_the_format_is_the_one_saved_for_that_source(self, saved):
        """WebUI 按来源分别记画幅；默认值得是当前来源那一个。"""
        form = new_video.form()
        assert form["values"]["video_aspect"] == "16:9"
        assert form["saved_aspects"] == {"openai_image": "16:9"}

    def test_reading_the_form_writes_nothing(self, saved):
        new_video.form()
        new_video.build_params({"video_subject": "cats"})
        assert saved["written"] == {"ui": {}, "app": {}}
        saved["save"].assert_not_called()

    def test_a_saved_value_that_is_no_longer_valid_does_not_block_the_form(
        self, saved
    ):
        config.ui["video_clip_duration"] = 99   # API 上限是 15
        assert 1 <= new_video.form()["values"]["video_clip_duration"] <= 15

    def test_every_default_is_a_value_the_api_will_take_back(self, saved):
        """表单原样交回默认值也得能开片子——否则"什么都不改就点创建"会被拒。"""
        form = new_video.form()
        sendable = {name: value for name, value in form["values"].items()
                    if name in NewVideoRequest.model_fields}
        body = NewVideoRequest(video_subject="cats", **sendable)
        new_video.build_params(body.model_dump())

    def test_every_field_in_the_form_is_one_the_api_accepts(self, saved):
        names = set(_fields(new_video.form()))
        assert names <= set(NewVideoRequest.model_fields), (
            names - set(NewVideoRequest.model_fields)
        )

    def test_a_field_only_depends_on_fields_that_exist(self, saved):
        form = new_video.form()
        known = set(form["values"])
        for field in _fields(form).values():
            assert set(field.get("show", {})) <= known, field["name"]

    def test_the_languages_are_the_same_list_the_creation_page_offers(self):
        source = (ROOT / "webui" / "Main.py").read_text(encoding="utf-8")
        block = re.search(r"^support_locales = \[(.*?)^\]", source, re.S | re.M)
        assert block, "support_locales moved; update new_video.LANGUAGES"
        assert tuple(re.findall(r'"([^"]+)"', block.group(1))) == new_video.LANGUAGES


class TestItCoversWhatTheOldPageOffered:
    """WebUI 点生成之前能设的东西。少一类，那一类就还得回 :8501 去设。"""

    def test_every_source_of_the_old_page_is_here_or_refused_by_name(self):
        source = (ROOT / "webui" / "Main.py").read_text(encoding="utf-8")
        block = re.search(r"^VIDEO_SOURCE_GROUPS = \{(.*?)^\}", source, re.S | re.M)
        old = set(re.findall(r'"([a-z_0-9]+)"', block.group(1))) - {
            "stock_video", "ai_video", "ai_image", "local",
        } | {"local"}
        # 胜算云要先询价再确认扣费，那套流程还没搬；它是唯一的例外，而且是
        # 点名的例外——多出第二个就该在这里红。
        assert old - set(new_video.SOURCE_IDS) == {"loomloom"}

    def test_every_voice_service_of_the_old_page_is_here(self):
        source = (ROOT / "webui" / "Main.py").read_text(encoding="utf-8")
        block = re.search(r"tts_servers = \[(.*?)\n            \]", source, re.S)
        old = re.findall(r'\("([a-z0-9_-]+)", "', block.group(1))
        assert set(old) == set(new_video.TTS_SERVER_IDS)

    def test_every_setting_the_old_page_remembered_can_be_set_here(self, saved):
        """WebUI 存进 ``[ui]`` 的、和任务参数同名的那些键。"""
        fields = set(_fields(new_video.form())) | {
            "video_language", "paragraph_number",   # 在左边那一栏
        }
        for name in new_video.UI_FIELDS + new_video.APP_FIELDS:
            assert name in fields, name

    def test_what_people_change_every_time_is_never_behind_more_settings(
        self, saved
    ):
        fields = _fields(new_video.form())
        for name in ("video_source", "confirm_charge", "local_materials",
                     "video_aspect", "image_style", "video_clip_duration",
                     "voice_mode", "tts_server", "voice_name", "voice_rate",
                     "custom_audio", "subtitle_enabled", "font_name", "font_size",
                     "text_fore_color", "subtitle_position", "music_type",
                     "music_preset", "music_custom", "bgm_volume"):
            assert not fields[name].get("advanced"), name

    def test_every_tab_keeps_a_short_first_screen(self, saved):
        """一页摆十七项，没人找得到"画面从哪来"。"""
        for tab in new_video.form()["tabs"]:
            if tab.get("side"):
                continue
            usual = [f for f in tab["fields"] if not f.get("advanced")]
            assert 1 <= len(usual) <= 8, (tab["id"], len(usual))

    def test_more_settings_names_only_fields_that_exist(self, saved):
        assert new_video.ADVANCED_FIELDS <= set(_fields(new_video.form()))

    def test_the_three_kinds_of_narration(self, saved):
        mode = _fields(new_video.form())["voice_mode"]
        assert [one["id"] for one in mode["options"]] == ["tts", "upload", "none"]

    def test_the_six_kinds_of_music(self, saved):
        music = _fields(new_video.form())["music_type"]
        assert [one["id"] for one in music["options"]] == [
            "", "random", "preset", "custom", "sonilo", "elevenlabs",
        ]


class TestVoices:
    def test_they_come_grouped_by_language_with_the_gender_said(self, saved):
        got = new_video.voices("kokoro", prefer="en-US")
        assert got["groups"][0]["label"] == "English"
        assert {"id": "kokoro:am_adam", "label": "am_adam (male)"} in (
            got["groups"][0]["voices"]
        )

    def test_a_service_that_does_not_answer_is_named_not_replaced(self, saved):
        with patch.object(new_video.voice, "get_kokoro_voices", return_value=[]):
            got = new_video.voices("kokoro", keep="kokoro:am_adam")
        assert any("Kokoro" in note for note in got["notes"])
        # 已经选着的那个还在：断线不等于用户改了主意。
        assert got["groups"][0]["voices"][0]["id"] == "kokoro:am_adam"
        # 而且没有拿别家的音色来顶。
        assert len(got["groups"]) == 1

    def test_a_service_that_raises_is_reported_the_same_way(self, saved):
        with patch.object(new_video.voice, "get_elevenlabs_voices",
                          side_effect=RuntimeError("401")), \
                patch.object(new_video.voice, "get_elevenlabs_api_key",
                             return_value=""):
            got = new_video.voices("elevenlabs")
        assert got["groups"] == []
        assert "ElevenLabs" in got["notes"][0]

    def test_an_unknown_service_is_refused(self, saved):
        with pytest.raises(new_video.NewVideoError, match="nope"):
            new_video.voices("nope")

    def test_edge_and_azure_v2_do_not_share_a_list(self, saved):
        with patch.object(new_video.voice, "get_all_azure_voices",
                          return_value=["en-US-AvaNeural-Female",
                                        "en-US-AvaMultilingualNeural-V2-Female"]), \
                patch.object(new_video.voice, "is_azure_v2_voice",
                             side_effect=lambda name: "V2" in name):
            edge = new_video.voices("azure-tts-v1")
            azure = new_video.voices("azure-tts-v2")
        ids = lambda got: [v["id"] for g in got["groups"] for v in g["voices"]]
        assert ids(edge) == ["en-US-AvaNeural-Female"]
        assert ids(azure) == ["en-US-AvaMultilingualNeural-V2-Female"]


class TestWhatIsChosenBecomesTheVideo:
    def test_untouched_fields_keep_the_saved_settings(self, saved):
        params = new_video.build_params({"video_subject": "cats"})
        assert params.video_subject == "cats"
        assert params.voice_name == "kokoro:am_adam"
        assert params.font_name == "MicrosoftYaHeiBold.ttc"
        assert params.text_fore_color == "#FFD700"
        assert params.video_aspect.value == "16:9"
        assert params.bgm_type == "random"

    def test_what_the_form_sets_wins_over_what_was_saved(self, saved):
        params = new_video.build_params({
            "video_subject": "cats", "video_aspect": "1:1",
            "voice_name": "en-US-AvaNeural-Female", "voice_rate": 1.2,
            "subtitle_enabled": False, "video_language": "",
            "video_clip_duration": 7, "music_type": "", "bgm_volume": 0.5,
            "video_transition_mode": "FadeIn", "video_fit_mode": "contain",
            "video_concat_mode": "sequential", "video_count": 2,
            "font_size": 90, "subtitle_position": "custom", "custom_position": 40,
            "transition_sound": "whoosh.mp3", "video_terms": "cat, nap",
            "llm_provider": "ollama", "llm_model": "llama3",
        })
        assert params.video_aspect.value == "1:1"
        assert params.voice_name == "en-US-AvaNeural-Female"
        assert params.voice_rate == 1.2
        assert params.subtitle_enabled is False
        # 空字符串是"自动"，是一个选择，不是"没填"。
        assert params.video_language == ""
        assert params.video_clip_duration == 7
        assert (params.bgm_type, params.bgm_file) == ("", "")
        assert params.bgm_volume == 0.5
        assert params.video_transition_mode.value == "FadeIn"
        assert params.video_fit_mode.value == "contain"
        assert params.video_concat_mode.value == "sequential"
        assert params.video_count == 2
        assert (params.font_size, params.custom_position) == (90, 40)
        assert params.transition_sound == "whoosh.mp3"
        assert params.video_terms == "cat, nap"
        assert (params.llm_provider, params.llm_model) == ("ollama", "llama3")

    def test_no_transition_is_a_choice_too(self, saved):
        config.ui["video_transition_mode"] = "FadeIn"
        params = new_video.build_params(
            {"video_subject": "cats", "video_transition_mode": "None"}
        )
        assert params.video_transition_mode.value == "None"

    def test_the_picture_style_is_pinned_on_the_video_not_on_the_settings(
        self, saved
    ):
        params = new_video.build_params(
            {"video_subject": "cats", "image_style": "anime"}
        )
        assert params.image_prompt_template == (
            image_style_model.template_for_style("anime")
        )
        assert "openai_image_prompt_template" not in config.app

    def test_no_narration(self, saved):
        params = new_video.build_params(
            {"video_subject": "cats", "voice_mode": "none"}
        )
        assert params.voice_name == new_video.voice.NO_VOICE_NAME

    def test_reading_it_needs_a_voice(self, saved):
        with pytest.raises(new_video.NewVideoError, match="voice"):
            new_video.build_params(
                {"video_subject": "cats", "voice_mode": "tts", "voice_name": ""}
            )

    def test_a_box_behind_the_subtitles(self, saved):
        on = new_video.build_params({
            "video_subject": "cats", "subtitle_background_enabled": True,
            "subtitle_background_color": "#112233",
        })
        assert on.text_background_color == "#112233"
        off = new_video.build_params(
            {"video_subject": "cats", "subtitle_background_enabled": False}
        )
        assert off.text_background_color is False

    def test_music_from_the_library_from_a_file_and_composed(self, saved):
        preset = new_video.build_params({
            "video_subject": "c", "music_type": "preset", "music_preset": "calm.mp3",
        })
        assert preset.bgm_type == "preset" and preset.bgm_file.endswith("calm.mp3")
        own = new_video.build_params({
            "video_subject": "c", "music_type": "custom", "music_custom": "mine.mp3",
            # 上一次选的库里那首不该串到"我自己的文件"里来。
            "music_preset": "calm.mp3",
        })
        assert own.bgm_type == "custom" and own.bgm_file.endswith("mine.mp3")
        composed = new_video.build_params({
            "video_subject": "c", "music_type": "sonilo", "music_prompt": "lo-fi",
        })
        assert (composed.bgm_type, composed.video_music_prompt) == ("sonilo", "lo-fi")

    def test_a_named_track_is_checked_before_the_video_starts(self, saved):
        """等渲染完才发现没有这首曲子，用户已经白等了一整条片子。"""
        with pytest.raises(new_video.NewVideoError, match="nope.mp3"):
            new_video.build_params({
                "video_subject": "c", "music_type": "preset",
                "music_preset": "nope.mp3",
            })

    def test_my_own_files_become_the_materials(self, saved):
        folder = saved["storage"] / "local_videos"
        folder.mkdir(parents=True)
        (folder / "a.mp4").write_bytes(b"x")
        params = new_video.build_params({
            "video_subject": "c", "video_source": "local",
            "local_materials": ["a.mp4"],
        })
        assert [(one.provider, os.path.basename(one.url))
                for one in params.video_materials] == [("local", "a.mp4")]

    @pytest.mark.parametrize("name", ["../secret.mp4", "gone.mp4", "C:/x.mp4"])
    def test_only_files_that_were_uploaded_count(self, saved, name):
        (saved["storage"] / "local_videos").mkdir(parents=True)
        with pytest.raises(new_video.NewVideoError):
            new_video.build_params({
                "video_subject": "c", "video_source": "local",
                "local_materials": [name],
            })

    def test_neither_a_subject_nor_a_script_is_refused(self, saved):
        with pytest.raises(new_video.NewVideoError, match="both are empty"):
            new_video.build_params({"video_subject": "  ", "video_script": ""})

    def test_a_mistyped_field_is_refused_not_ignored(self):
        with pytest.raises(Exception):
            NewVideoRequest(video_subject="cats", voice_nmae="x")


class TestMyOwnRecording:
    def test_it_is_set_aside_then_moved_into_the_video(self, saved, tmp_path):
        token = new_video.stage_audio("Take 3.MP3", io.BytesIO(b"sound"))
        assert token.endswith(".mp3") and "Take" not in token
        new_video.build_params({
            "video_subject": "c", "voice_mode": "upload", "custom_audio": token,
        })
        task_dir = tmp_path / "task-1"
        task_dir.mkdir()
        with patch.object(new_video.utils, "task_dir", return_value=str(task_dir)):
            placed = new_video.place_audio("task-1", token)
        # 流水线只认任务目录里的文件。
        assert os.path.dirname(placed) == str(task_dir)
        assert Path(placed).read_bytes() == b"sound"

    def test_using_a_recording_needs_one(self, saved):
        with pytest.raises(new_video.NewVideoError, match="upload your recording"):
            new_video.build_params({"video_subject": "c", "voice_mode": "upload"})

    def test_a_file_that_is_not_sound_is_refused(self, saved):
        with pytest.raises(new_video.NewVideoError, match=".exe"):
            new_video.stage_audio("voice.exe", io.BytesIO(b"x"))

    def test_an_empty_file_is_refused_and_not_left_behind(self, saved):
        with pytest.raises(new_video.NewVideoError, match="empty"):
            new_video.stage_audio("voice.mp3", io.BytesIO(b""))
        assert not list((saved["storage"] / "new_video_audio").iterdir())

    @pytest.mark.parametrize("token", ["../../config.toml", "", "a/b.mp3"])
    def test_the_name_handed_back_cannot_reach_other_files(self, saved, token):
        with pytest.raises(new_video.NewVideoError):
            new_video.staged_audio(token)


class TestNoQuietSubstitutionAndNoQuietCharge:
    def test_a_source_the_form_does_not_offer_is_refused_by_name(self, saved):
        with pytest.raises(new_video.NewVideoError, match="loomloom"):
            new_video.build_params(
                {"video_subject": "cats", "video_source": "loomloom"}
            )

    @pytest.mark.parametrize("source", new_video.PAID_SOURCES)
    def test_a_charged_source_does_not_start_without_the_tick(self, saved, source):
        with pytest.raises(new_video.NewVideoError, match="charged"):
            new_video.build_params({"video_subject": "c", "video_source": source})
        with pytest.raises(new_video.NewVideoError, match="charged"):
            new_video.build_params({"video_subject": "c", "video_source": source,
                                    "confirm_charge": False})
        params = new_video.build_params({"video_subject": "c", "video_source": source,
                                         "confirm_charge": True})
        assert params.video_source == source

    def test_the_free_sources_need_no_tick(self, saved):
        for source in ("openai_image", "codex_image", "pexels", "pixabay", "coverr"):
            assert source not in new_video.PAID_SOURCES
            new_video.build_params({"video_subject": "c", "video_source": source})

    def test_an_unknown_style_is_refused(self, saved):
        with pytest.raises(new_video.NewVideoError, match="vaporwave"):
            new_video.build_params(
                {"video_subject": "cats", "image_style": "vaporwave"}
            )

    def test_a_style_on_stock_video_is_refused_rather_than_ignored(self, saved):
        with pytest.raises(new_video.NewVideoError, match="pexels"):
            new_video.build_params({"video_subject": "cats",
                                    "video_source": "pexels",
                                    "image_style": "anime"})

    def test_a_script_the_model_did_not_write_names_who_failed(self, saved):
        config.app["llm_provider"] = "claude_code"
        with patch("app.services.llm.generate_script",
                   return_value="Error: quota exceeded"):
            with pytest.raises(new_video.NewVideoError) as raised:
                new_video.write_script({"video_subject": "cats"})
        assert "claude_code" in str(raised.value)
        assert "quota exceeded" in str(raised.value)

    def test_a_voice_that_cannot_be_read_names_the_service(self, saved):
        with patch.object(new_video.voice, "tts", side_effect=RuntimeError("down")):
            with pytest.raises(new_video.NewVideoError, match="Kokoro"):
                new_video.preview_voice("kokoro:am_adam")


class TestItAlwaysStopsForReview:
    def test_generated_pictures_stop_at_the_prompts(self, saved):
        params = new_video.build_params({"video_subject": "cats"})
        assert new_video.stop_stage(params) == "subtitle"

    def test_the_form_can_skip_the_prompt_review_for_one_video(self, saved):
        params = new_video.build_params({"video_subject": "cats"})
        assert new_video.stop_stage(params, review_prompts=False) == "materials"

    def test_stock_video_stops_at_the_materials_whatever_the_switch_says(
        self, saved
    ):
        params = new_video.build_params(
            {"video_subject": "cats", "video_source": "pexels"}
        )
        assert new_video.stop_stage(params, review_prompts=True) == "materials"

    def test_no_source_ever_runs_straight_to_a_finished_video(self, saved):
        """``review_before_render = false`` 管的是 WebUI 那个按钮。从编辑器
        开的片子，成片由编辑器里的"渲染"决定。"""
        config.ui["review_before_render"] = False
        for source in new_video.SOURCE_IDS:
            if source == "local":
                continue
            params = new_video.build_params({
                "video_subject": "cats", "video_source": source,
                "confirm_charge": True,
            })
            for review in (None, True, False):
                assert new_video.stop_stage(params, review) != "video"


class TestTheChoicesAreRememberedLikeTheOldPageDid:
    def test_after_creating_the_next_form_starts_from_these_choices(self, saved):
        choices = {
            "video_subject": "cats", "video_aspect": "1:1", "voice_mode": "tts",
            "voice_name": "en-US-AvaNeural-Female", "voice_rate": 1.2,
            "music_type": "preset", "music_preset": "calm.mp3",
            "video_source": "pexels", "review_prompts": False,
            "subtitle_background_enabled": True,
            "subtitle_background_color": "#112233",
        }
        new_video.remember(choices, new_video.build_params(choices))
        saved["save"].assert_called_once()

        values = new_video.form()["values"]
        assert values["video_source"] == "pexels"
        assert values["video_aspect"] == "1:1"
        assert values["voice_name"] == "en-US-AvaNeural-Female"
        assert values["tts_server"] == "azure-tts-v1"
        assert values["voice_rate"] == 1.2
        assert (values["music_type"], values["music_preset"]) == ("preset", "calm.mp3")
        assert values["review_prompts"] is False
        assert values["subtitle_background_enabled"] is True

    def test_it_writes_the_keys_the_old_page_reads(self, saved):
        choices = {"video_subject": "cats", "video_transition_mode": "None"}
        new_video.remember(choices, new_video.build_params(choices))
        ui, app = saved["written"]["ui"], saved["written"]["app"]
        assert ui["video_aspect_openai_image"] == "16:9"
        assert ui["bgm_type"] == "random"
        assert ui["video_transition_mode"] == "None"
        assert app["video_source"] == "openai_image"
        # 只管这一条的东西不进设置。
        for one_off in ("video_subject", "video_script", "confirm_charge",
                        "local_materials", "custom_audio", "image_prompt_template"):
            assert one_off not in ui and one_off not in app

    def test_no_narration_does_not_wipe_the_voice_that_was_chosen_before(
        self, saved
    ):
        choices = {"video_subject": "cats", "voice_mode": "none"}
        new_video.remember(choices, new_video.build_params(choices))
        assert "voice_name" not in saved["written"]["ui"]
        assert config.ui["voice_name"] == "kokoro:am_adam"
        assert saved["written"]["ui"]["voice_mode"] == "none"


class TestWritingTheScriptFirst:
    def test_it_uses_the_prompts_and_the_chosen_language(self, saved):
        with patch("app.services.llm.generate_script",
                   return_value="Cats nap.") as write:
            script = new_video.write_script({
                "video_subject": "cats", "video_language": "fr-FR",
                "paragraph_number": 3, "video_script": "an old draft",
                "video_script_prompt": "Keep it punchy.",
            })
        assert script == "Cats nap."
        asked = write.call_args.kwargs
        assert asked["video_subject"] == "cats"
        assert asked["language"] == "fr-FR"
        assert asked["paragraph_number"] == 3
        assert asked["video_script_prompt"] == "Keep it punchy."

    def test_a_half_filled_form_does_not_stop_the_script(self, saved):
        """写脚本和"来源要不要确认扣费、配音传没传"无关。"""
        with patch("app.services.llm.generate_script", return_value="Cats nap."):
            assert new_video.write_script({
                "video_subject": "cats", "video_source": "wavespeed",
                "voice_mode": "upload", "music_type": "custom",
            }) == "Cats nap."

    def test_it_needs_a_subject(self, saved):
        with pytest.raises(new_video.NewVideoError, match="about"):
            new_video.write_script({"video_script": "only a draft"})


class TestOverHttp:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from app import asgi

        return TestClient(asgi.app)

    def test_the_form_is_served(self, client, saved):
        got = client.get("/api/v1/new-video/options")
        assert got.status_code == 200, got.text
        data = got.json()["data"]
        assert data["values"]["voice_name"] == "kokoro:am_adam"
        assert [tab["id"] for tab in data["tabs"]] == [
            "pictures", "voice", "subtitles", "music", "script",
        ]

    def test_the_voices_of_a_service_are_served(self, client, saved):
        got = client.get("/api/v1/new-video/voices",
                         params={"server": "kokoro", "language": "en-US"})
        assert got.status_code == 200, got.text
        assert got.json()["data"]["groups"][0]["label"] == "English"
        assert client.get("/api/v1/new-video/voices",
                          params={"server": "nope"}).status_code == 400

    def test_starting_hands_the_pipeline_the_merged_parameters(self, client, saved):
        from app.controllers.v1 import editor
        from app.utils import utils

        started = {}

        def fake_create(request, body, stop_at, task_id=None):
            started.update(body=body, stop_at=stop_at, task_id=task_id)
            return utils.get_response(200, {"task_id": task_id})

        with patch.object(editor, "create_task", side_effect=fake_create):
            got = client.post("/api/v1/new-video", json={
                "video_subject": "cats", "image_style": "anime",
                "review_prompts": False,
            })
        assert got.status_code == 200, got.text
        assert got.json()["data"]["task_id"] == started["task_id"]
        assert started["stop_at"] == "materials"
        assert isinstance(started["body"], TaskVideoRequest)
        assert started["body"].voice_name == "kokoro:am_adam"
        assert started["body"].image_prompt_template
        # 开成了才记。
        assert saved["written"]["ui"]["review_prompts_before_images"] is False

    def test_an_uploaded_recording_lands_in_the_new_videos_own_folder(
        self, client, saved, tmp_path
    ):
        from app.controllers.v1 import editor
        from app.utils import utils

        up = client.post("/api/v1/new-video/audio",
                         files={"file": ("take.mp3", b"sound", "audio/mpeg")})
        assert up.status_code == 200, up.text
        token = up.json()["data"]["file"]

        started = {}

        def fake_create(request, body, stop_at, task_id=None):
            started.update(body=body, task_id=task_id)
            return utils.get_response(200, {"task_id": task_id})

        tasks = tmp_path / "tasks"

        def task_dir(task_id=""):
            path = tasks / task_id
            path.mkdir(parents=True, exist_ok=True)
            return str(path)

        with patch.object(editor, "create_task", side_effect=fake_create), \
                patch.object(new_video.utils, "task_dir", side_effect=task_dir):
            got = client.post("/api/v1/new-video", json={
                "video_subject": "cats", "voice_mode": "upload",
                "custom_audio": token,
            })
        assert got.status_code == 200, got.text
        placed = Path(started["body"].custom_audio_file)
        assert placed.parent == tasks / started["task_id"]
        assert placed.read_bytes() == b"sound"

    def test_a_refusal_reaches_the_form_with_the_reason_and_remembers_nothing(
        self, client, saved
    ):
        from app.controllers.v1 import editor

        with patch.object(editor, "create_task") as create:
            got = client.post("/api/v1/new-video", json={
                "video_subject": "cats", "video_source": "wavespeed",
            })
        assert got.status_code == 400
        assert "charged" in got.json()["message"]
        create.assert_not_called()
        assert saved["written"] == {"ui": {}, "app": {}}

    def test_the_script_route_returns_the_script(self, client, saved):
        with patch("app.services.llm.generate_script", return_value="Cats nap."):
            got = client.post("/api/v1/new-video/script",
                              json={"video_subject": "cats"})
        assert got.status_code == 200, got.text
        assert got.json()["data"]["video_script"] == "Cats nap."

    def test_the_sample_is_played_and_then_thrown_away(self, client, saved):
        made = {}

        def fake_tts(text, voice_name, voice_rate, voice_file, voice_volume):
            Path(voice_file).write_bytes(b"mp3")
            made.update(path=voice_file, text=text)
            return object()

        with patch.object(new_video.voice, "tts", side_effect=fake_tts):
            got = client.post("/api/v1/new-video/voice-preview", json={
                "voice_name": "kokoro:am_adam", "text": "  Cats   nap. ",
            })
        assert got.status_code == 200, got.text
        assert got.content == b"mp3"
        assert made["text"] == "Cats nap."
        assert not os.path.exists(made["path"])

    def test_the_old_ways_of_starting_a_video_are_untouched(self, client):
        """加，不是换：``/videos`` 和 ``/materials`` 还在，停的地方也没变。"""
        from app.controllers.v1 import editor, video

        routes = {(route.path, tuple(sorted(route.methods)))
                  for route in client.app.routes if hasattr(route, "methods")}
        assert ("/api/v1/videos", ("POST",)) in routes
        assert ("/api/v1/materials", ("POST",)) in routes
        with patch.object(video, "create_task", return_value={}) as create:
            video.create_video(None, None, "body")
        assert create.call_args.kwargs == {"stop_at": "video"}
        with patch.object(editor, "create_task", return_value={}) as create:
            editor.create_review_task(None, "body")
        assert create.call_args.kwargs == {"stop_at": "materials"}


class TestTheEditorOffersIt:
    """界面那一半。编辑器没有构建步骤和测试运行器，所以钉的是源码本身。"""

    @staticmethod
    def _read(name: str) -> str:
        return (ROOT / "resource" / "public" / "editor" / name).read_text(
            encoding="utf-8"
        )

    @staticmethod
    def _body(source: str, name: str) -> str:
        start = source.index(f"function {name}(")
        ends = [at for at in (source.find("\nfunction ", start + 1),
                              source.find("\nasync function ", start + 1)) if at > 0]
        return source[start:min(ends)]

    def test_new_video_is_in_the_top_bar_and_on_the_videos_screen(self):
        page = self._read("index.html")
        bar = page[page.index('<header class="bar">'):page.index("</header>")]
        assert 'id="new-video"' in bar and 'id="app-settings"' in bar
        library = page[page.index('<section id="library"'):]
        assert 'id="library-new"' in library[:library.index("</section>")]
        script = self._read("editor.js")
        assert 'ui.newVideo.addEventListener("click", openNewVideo);' in script
        assert 'ui.libraryNew.addEventListener("click", openNewVideo);' in script

    def test_nothing_in_the_editor_sends_you_to_the_old_page(self):
        """整件事的目的：一个端口。"""
        script = self._read("editor.js")
        page = self._read("index.html")
        assert "8501" not in script
        assert not re.search(r'href="[^"]*8501', page)
        assert 'ui.newSettings.addEventListener("click"' in script
        wired = script[script.index('ui.newSettings.addEventListener("click"'):]
        assert "openSettings();" in wired[:wired.index("});")]

    def test_every_element_the_script_looks_up_is_on_the_page(self):
        page = self._read("index.html")
        for dom_id in re.findall(r'el\("([a-z-]+)"\)', self._read("editor.js")):
            assert f'id="{dom_id}"' in page, dom_id

    def test_the_form_is_drawn_from_what_the_server_says(self):
        script = self._read("editor.js")
        assert 'call("GET", "/new-video/options")' in self._body(
            script, "openNewVideo"
        )
        draw = self._body(script, "drawNewVideo")
        assert "lookTabs()" in draw and "newField(field)" in draw
        assert "fresh.form.tabs" in self._body(script, "lookTabs")
        assert "formField(field" in self._body(script, "newField")
        # 页面里没有第二份"能挑哪些来源/哪些配音服务"的清单。
        for listed in ("wavespeed", "azure-tts-v1", "pexels", "elevenlabs"):
            assert listed not in script, listed

    def test_every_kind_of_field_the_server_sends_can_be_drawn(self, saved):
        kinds = {field["kind"] for field in _fields(new_video.form()).values()}
        control = self._body(self._read("editor.js"), "formField")
        for kind in kinds - {"text"}:    # text 是兜底那一支
            assert f'"{kind}"' in control, kind

    def test_more_settings_folds_the_screen_not_the_feature(self):
        """收起来的项照样发出去：收的是屏幕，不是功能。否则"没点开更多设置"
        的人会悄悄丢掉上次存的转场、描边、转场音。"""
        script = self._read("editor.js")
        choices = self._body(script, "newVideoChoices")
        assert "advanced" not in choices and "fresh.more" not in choices
        draw = self._body(script, "drawNewVideo")
        assert "filter((field) => field.advanced)" in draw
        assert "fresh.more[tab.id] = !open;" in draw

    def test_the_script_settings_sit_under_the_script_not_in_a_tab(self):
        script = self._read("editor.js")
        assert "filter((tab) => !tab.side)" in self._body(script, "lookTabs")
        draw = self._body(script, "drawNewVideo")
        assert "ui.newScriptFields.appendChild(newField(field))" in draw
        page = self._read("index.html")
        left = page[page.index('id="new-script"'):page.index('id="new-tabs"')]
        assert 'id="new-script-more"' in left

    def test_a_hidden_field_is_not_sent(self):
        """库存视频没有画风；把收起来的那一项发出去只会被拒绝。"""
        body = self._body(self._read("editor.js"), "newVideoChoices")
        assert "if (!fieldShown(field, fresh.values)) continue;" in body
        assert body.index("fieldShown(") < body.index("choices[field.name] = value")

    def test_changing_source_takes_back_the_charge_confirmation(self):
        body = self._body(self._read("editor.js"), "setNewValue")
        source = body[body.index('name === "video_source"'):]
        assert "fresh.values.confirm_charge = false;" in source[:source.index("\n  }")]

    def test_the_uploads_and_the_tick_do_not_carry_over_to_the_next_video(self):
        body = self._body(self._read("editor.js"), "createNewVideo")
        reset = body[body.index("Object.assign(fresh.values"):]
        reset = reset[:reset.index("});")]
        for one_off in ("confirm_charge: false", "local_materials: []",
                        'custom_audio: ""'):
            assert one_off in reset, one_off

    def test_a_slow_voice_list_does_not_overwrite_a_newer_choice(self):
        body = self._body(self._read("editor.js"), "loadVoices")
        guard = "if (fresh.values.tts_server !== server) return;"
        assert guard in body
        assert body.index(guard) < body.index("fresh.values.voice_name = ")

    def test_it_waits_for_the_scenes_before_opening_the_timeline(self):
        """做到一半时还没有场景清单，直接 load 只会得到一句报错。"""
        script = self._read("editor.js")
        create = self._body(script, "createNewVideo")
        assert 'call("POST", "/new-video", choices)' in create
        assert "watchCreation(taskId" in create
        assert "load(" not in create
        watch = self._body(script, "watchCreation")
        assert watch.index("stateCode !== 4") < watch.index("await load(taskId)")

    def test_a_failure_is_shown_with_its_reason_not_as_a_spinner(self):
        watch = self._body(self._read("editor.js"), "watchCreation")
        failed = watch[watch.index("stateCode === -1"):watch.index("stateCode !== 4")]
        assert "task.error" in failed
        assert "return;" in failed

    def test_it_does_not_take_over_a_video_the_user_went_off_to_edit(self):
        watch = self._body(self._read("editor.js"), "watchCreation")
        guard = "ui.createModal.hidden && state.taskId && state.taskId !== taskId"
        assert guard in watch
        assert watch.index(guard) < watch.index("await load(taskId)")

    def test_a_reload_picks_the_new_video_back_up(self):
        script = self._read("editor.js")
        assert "localStorage.setItem(CREATING_KEY, taskId)" in self._body(
            script, "createNewVideo"
        )
        assert "watchCreation(taskId" in self._body(script, "resumeCreation")
        boot = script[script.index("const initial = new URLSearchParams"):]
        assert "resumeCreation();" in boot[:boot.index("window.addEventListener")]

    def test_embedded_in_the_old_page_it_does_not_offer_a_second_way_in(self):
        """嵌在 WebUI 里时，开片子和设置的入口就是外面那一页。"""
        script = self._read("editor.js")
        embedded = script[script.index("if (EMBEDDED) {"):]
        embedded = embedded[:embedded.index("}\n")]
        for hidden in ("ui.newVideo", "ui.libraryNew", "ui.appSettings"):
            assert hidden in embedded, hidden
