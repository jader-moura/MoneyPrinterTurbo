"""画风跟着任务走：出片时用的那个风格，重生成时还是它。

Why this file exists
--------------------
画风原本只存在 ``config.toml`` 里，也就是"现在"的设置。审片时在时间线编辑器
里重生成一张图跑的是 API 进程，读的是它启动时载入的那一份配置——于是用漫画
风出的片子，补拍出来的是一张油画。这组测试把"用哪个画风"这件事钉死在任务上。

The style used to live only in config.toml, which is the *current* setting. The
timeline editor regenerates through the API process, holding its own loaded copy
of that config, so a comic-book video could gain an oil painting. These tests
pin the answer to "which style" onto the task itself.
"""

from unittest.mock import patch

import pytest

from app.config import config
from app.models import image_style as image_style_model
from app.models.schema import VideoParams
from app.services import material, scene_editor, scene_manifest, task, task_artifacts
from app.services import state as sm
from app.utils import utils

COMIC = image_style_model.template_for_style("comic")
OIL = image_style_model.template_for_style("oil_painting")


@pytest.fixture
def task_root(tmp_path, monkeypatch):
    root = tmp_path / "tasks"

    def fake_task_dir(sub_dir: str = ""):
        target = root / sub_dir if sub_dir else root
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(utils, "task_dir", fake_task_dir)
    return root


@pytest.fixture
def settings_say_oil_painting(monkeypatch):
    """全局配置是油画，也就是"现在"的设置和出片时不一样的那种情况。"""
    monkeypatch.setitem(config.app, "openai_image_prompt_template", OIL)


@pytest.fixture(autouse=True)
def idle_task():
    with patch.object(sm.state, "get_task", return_value=None):
        yield


def _reviewed_task(task_root, task_id, *, pinned_template="", source="openai_image"):
    """造一个已经跑到素材阶段、等着审片的任务。"""
    params = VideoParams(
        video_subject="seeds",
        video_script="One. Two.",
        video_source=source,
        video_clip_duration=5,
        image_prompt_template=pinned_template,
    )
    task_artifacts.write_script_data(
        task_id,
        {
            "script": "One. Two.",
            "search_terms": ["a", "b"],
            "params": params.model_dump(),
        },
    )
    directory = task_root / task_id
    directory.mkdir(parents=True, exist_ok=True)
    materials = []
    for index in range(2):
        target = directory / f"{index + 1}.png"
        target.write_bytes(b"x")
        materials.append(str(target))
    audio = directory / "audio.mp3"
    audio.write_bytes(b"x")
    scene_manifest.record_prepared_artifacts(
        task_id,
        video_script="One. Two.",
        video_terms=["a", "b"],
        materials=materials,
        audio_file=str(audio),
        audio_duration=8.0,
        subtitle_path="",
        video_source=source,
        clip_duration=5,
    )
    return task_id, materials


def _regenerate(task_id, task_root, **kwargs):
    """重生成一次，把传给素材层的参数截下来。"""
    replacement = task_root / task_id / "new.png"
    replacement.write_bytes(b"y")
    captured = {}

    def fake_download_videos(**call_kwargs):
        captured.update(call_kwargs)
        return [str(replacement)]

    with patch.object(material, "download_videos", side_effect=fake_download_videos):
        scene = scene_editor.regenerate_material(task_id, 0, **kwargs)
    return scene, captured


class TestWhichStyleARegenerationUses:
    def test_it_is_the_one_the_video_was_made_with_not_the_current_setting(
        self, task_root, settings_say_oil_painting
    ):
        """这就是那个 bug：漫画风的片子，补拍出来是油画。"""
        task_id, _ = _reviewed_task(task_root, "pinned", pinned_template=COMIC)

        _, captured = _regenerate(task_id, task_root)

        assert captured["image_prompt_template"] == COMIC

    def test_a_task_from_before_this_field_falls_back_to_the_setting(
        self, task_root, settings_say_oil_painting
    ):
        """老任务没记过画风，那时唯一能说的就是"按设置来"，而不是报错。"""
        task_id, _ = _reviewed_task(task_root, "old", pinned_template="")

        _, captured = _regenerate(task_id, task_root)

        # 空字符串让素材层回到配置，与这个字段上线前的行为完全一致。
        assert captured["image_prompt_template"] == ""

    def test_an_explicit_style_wins_over_the_pinned_one(
        self, task_root, settings_say_oil_painting
    ):
        """用户就是想给这一张换个画风。"""
        task_id, _ = _reviewed_task(task_root, "explicit", pinned_template=COMIC)

        scene, captured = _regenerate(
            task_id, task_root, image_style="watercolour"
        )

        assert captured["image_prompt_template"] == (
            image_style_model.template_for_style("watercolour")
        )
        # 挑过的画风记在这一段上，界面下次打开还显示它。
        assert scene["image_style"] == "watercolour"

    def test_the_choice_sticks_for_the_next_regeneration_of_that_scene(
        self, task_root, settings_say_oil_painting
    ):
        task_id, _ = _reviewed_task(task_root, "sticky", pinned_template=COMIC)
        _regenerate(task_id, task_root, image_style="anime")

        _, captured = _regenerate(task_id, task_root)

        assert captured["image_prompt_template"] == (
            image_style_model.template_for_style("anime")
        )

    def test_an_unknown_style_is_refused_instead_of_silently_substituted(
        self, task_root
    ):
        """悄悄换一种画风正是这里要修的毛病，所以不静默回退。"""
        task_id, _ = _reviewed_task(task_root, "unknown", pinned_template=COMIC)

        with pytest.raises(scene_editor.SceneError) as error:
            _regenerate(task_id, task_root, image_style="bauhaus")

        assert "bauhaus" in str(error.value)
        assert "comic" in str(error.value)


class TestWhatTheEditorIsTold:
    def test_it_gets_the_style_list_and_the_one_this_video_used(
        self, task_root, settings_say_oil_painting
    ):
        task_id, _ = _reviewed_task(task_root, "described", pinned_template=COMIC)

        payload = scene_editor.describe(task_id)

        assert payload["image_style"] == "comic"
        assert payload["image_style_pinned"] is True
        assert "oil_painting" in payload["image_styles"]

    def test_a_task_from_before_this_field_is_marked_as_unpinned(
        self, task_root, settings_say_oil_painting
    ):
        """界面得分得清"和这条片子一样"和"按设置来"，否则就是在撒谎。"""
        task_id, _ = _reviewed_task(task_root, "unpinned", pinned_template="")

        payload = scene_editor.describe(task_id)

        assert payload["image_style_pinned"] is False
        assert payload["image_style"] == "oil_painting"

    def test_a_stock_footage_task_gets_no_styles_at_all(self, task_root):
        """Pexels 搜来的片子没有画风可挑，界面据此不画那个下拉框。"""
        task_id, _ = _reviewed_task(task_root, "stock", source="pexels")

        assert scene_editor.describe(task_id)["image_styles"] == []


class TestPinningAtTaskStart:
    def test_the_style_in_force_is_written_onto_the_task(
        self, settings_say_oil_painting
    ):
        params = VideoParams(video_subject="x", video_source="openai_image")

        task.pin_image_prompt_template(params)

        assert params.image_prompt_template == OIL

    def test_a_style_submitted_with_the_task_is_not_overwritten(
        self, settings_say_oil_painting
    ):
        """界面上选的那个才算，配置不能反过来盖掉它。"""
        params = VideoParams(
            video_subject="x",
            video_source="openai_image",
            image_prompt_template=COMIC,
        )

        task.pin_image_prompt_template(params)

        assert params.image_prompt_template == COMIC

    def test_stock_footage_tasks_are_left_alone(self, settings_say_oil_painting):
        params = VideoParams(video_subject="x", video_source="pexels")

        task.pin_image_prompt_template(params)

        assert params.image_prompt_template == ""


class TestThePromptTheModelActuallySees:
    def test_the_pinned_template_beats_the_current_setting(
        self, settings_say_oil_painting
    ):
        prompt = material._openai_image_prompt("a jar of coins", COMIC)

        assert "comic book art" in prompt
        assert "oil painting" not in prompt
        assert "a jar of coins" in prompt

    def test_no_template_means_the_setting_as_before(self, settings_say_oil_painting):
        assert "oil painting" in material._openai_image_prompt("a jar of coins")

    def test_download_videos_hands_it_to_the_image_generator(
        self, tmp_path, monkeypatch, settings_say_oil_painting
    ):
        """从素材编排到出图这一路不能把模板丢掉。"""
        monkeypatch.setattr(utils, "task_dir", lambda sub_dir="": str(tmp_path))
        seen = {}

        def fake_generator(**kwargs):
            seen.update(kwargs)
            return []

        material._download_videos_openai_image_on_demand(
            task_id="t",
            search_terms=["a jar of coins"],
            video_aspect=material.VideoAspect.portrait,
            audio_duration=5.0,
            max_clip_duration=5,
            material_directory=str(tmp_path),
            image_generator=fake_generator,
            prompt_template=COMIC,
        )

        assert seen["prompt_template"] == COMIC


class TestOverHttp:
    """响应模型不能把这几个新字段滤掉，请求体也要认 ``image_style``。"""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from app import asgi

        return TestClient(asgi.app)

    def test_the_timeline_carries_the_styles_to_the_editor(
        self, client, task_root, settings_say_oil_painting
    ):
        task_id, _ = _reviewed_task(task_root, "http-list", pinned_template=COMIC)

        data = client.get(f"/api/v1/tasks/{task_id}/timeline").json()["data"]

        assert data["image_style"] == "comic"
        assert data["image_style_pinned"] is True
        assert "watercolour" in data["image_styles"]

    def test_a_style_posted_for_one_scene_reaches_the_image_generator(
        self, client, task_root, settings_say_oil_painting
    ):
        task_id, _ = _reviewed_task(task_root, "http-post", pinned_template=COMIC)
        replacement = task_root / task_id / "new.png"
        replacement.write_bytes(b"y")
        captured = {}

        def fake_download_videos(**kwargs):
            captured.update(kwargs)
            return [str(replacement)]

        with patch.object(
            material, "download_videos", side_effect=fake_download_videos
        ):
            response = client.post(
                f"/api/v1/tasks/{task_id}/scenes/0/material",
                json={"keyword": "a jar of coins", "image_style": "stickman"},
            )

        assert response.status_code == 200, response.text
        assert captured["image_prompt_template"] == (
            image_style_model.template_for_style("stickman")
        )
        assert response.json()["data"]["image_style"] == "stickman"


class TestGoingBackToTheVideosStyle:
    """下拉框里选回"和这条片子一样"必须真的回得去。"""

    def test_an_empty_choice_returns_to_the_pinned_style_and_forgets_the_old_one(
        self, task_root, settings_say_oil_painting
    ):
        task_id, _ = _reviewed_task(task_root, "cleared", pinned_template=COMIC)
        _regenerate(task_id, task_root, image_style="anime")

        scene, captured = _regenerate(task_id, task_root, image_style="")

        assert captured["image_prompt_template"] == COMIC
        # 记着的选择也要清掉，否则界面下次打开显示的还是动画风。
        assert "image_style" not in scene
        assert "image_style" not in scene_manifest.load(task_id)["scenes"][0]


class TestAddingAScene:
    """补一张画面同样要能挑画风，默认跟着这条片子。"""

    def _add(self, task_id, task_root, **kwargs):
        new_material = task_root / task_id / "added.png"
        new_material.write_bytes(b"z")
        captured = {}

        def fake_download_videos(**call_kwargs):
            captured.update(call_kwargs)
            return [str(new_material)]

        with patch.object(
            material, "download_videos", side_effect=fake_download_videos
        ):
            scene = scene_editor.add_scene(
                task_id, keyword="a jar of coins", **kwargs
            )
        return scene, captured

    def test_by_default_it_matches_the_rest_of_the_video(
        self, task_root, settings_say_oil_painting
    ):
        task_id, _ = _reviewed_task(task_root, "added-default", pinned_template=COMIC)

        _, captured = self._add(task_id, task_root)

        assert captured["image_prompt_template"] == COMIC

    def test_a_chosen_style_is_used_and_remembered_on_that_scene(
        self, task_root, settings_say_oil_painting
    ):
        task_id, _ = _reviewed_task(task_root, "added-style", pinned_template=COMIC)

        scene, captured = self._add(
            task_id, task_root, image_style="watercolour"
        )

        assert captured["image_prompt_template"] == (
            image_style_model.template_for_style("watercolour")
        )
        # 记住了，所以之后重生成这一段不会变回漫画风。
        assert scene["image_style"] == "watercolour"
        stored = scene_manifest.load(task_id)["scenes"][scene["index"]]
        assert stored["image_style"] == "watercolour"

    def test_an_unknown_style_is_refused_before_anything_is_generated(
        self, task_root
    ):
        task_id, materials = _reviewed_task(
            task_root, "added-unknown", pinned_template=COMIC
        )

        with pytest.raises(scene_editor.SceneError) as error:
            self._add(task_id, task_root, image_style="bauhaus")

        assert "bauhaus" in str(error.value)
        # 时间线不能留下半条新场景。
        assert len(scene_manifest.load(task_id)["scenes"]) == len(materials)


    def test_a_style_posted_with_a_new_scene_is_used(
        self, task_root, settings_say_oil_painting
    ):
        """界面上那个加号走的是 HTTP，所以请求体也要认 ``image_style``。"""
        from fastapi.testclient import TestClient

        from app import asgi

        task_id, _ = _reviewed_task(task_root, "added-http", pinned_template=COMIC)
        new_material = task_root / task_id / "added.png"
        new_material.write_bytes(b"z")
        captured = {}

        def fake_download_videos(**kwargs):
            captured.update(kwargs)
            return [str(new_material)]

        with patch.object(
            material, "download_videos", side_effect=fake_download_videos
        ):
            response = TestClient(asgi.app).post(
                f"/api/v1/tasks/{task_id}/scenes",
                json={"keyword": "a jar of coins", "image_style": "anime"},
            )

        assert response.status_code == 200, response.text
        assert captured["image_prompt_template"] == (
            image_style_model.template_for_style("anime")
        )
        assert response.json()["data"]["image_style"] == "anime"
