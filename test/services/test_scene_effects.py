"""每一段画面自己的转场和运镜。

Why these tests exist
---------------------
转场和运镜原来都是整条片子一个设置。用户在审片界面能看见每一段，却只能对
整条片子说话：想让开头那张淡入、其余直接切，做不到。

两者在片子里的位置完全不同，这也是这组测试要钉住的区别：

* **转场**是渲染期的效果，``process_one_clip`` 逐段应用。所以它只要存下来，
  再随剪辑表交给渲染器——而剪辑表只有"编辑过"的任务才会写，于是挑过转场的
  任务也必须开始写剪辑表，否则那段设置永远到不了渲染器手上。
* **运镜**在出图那一刻就烘进 mp4 了（``render_image_zoom_video``），渲染期
  根本没有它。所以改它只能从原图重烘一段。

两者都用空字符串表示"跟整条片子一样"，而这和显式选 ``none``（这一刀不要
转场）必须是两回事：前者跟着片子走，后者压过片子的设置。一样都没挑过时，
整条任务不写剪辑表，出片结果与本功能上线前逐字节一致。
"""

import os
from unittest.mock import patch

import pytest

from app.models.schema import VideoTransitionMode
from app.services import scene_editor, scene_manifest, task_artifacts, timeline
from app.services import video as video_service

from test.services.test_scene_editor import _reviewed_task, idle_task, task_root  # noqa: F401


class TestChoosingATransition:
    def test_a_scene_remembers_the_transition_it_was_given(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        scene_editor.set_effects(task_id, 0, transition="fade_in")
        assert scene_manifest.load(task_id)["scenes"][0]["transition"] == (
            VideoTransitionMode.fade_in.value
        )

    def test_the_read_model_reports_it_by_its_own_id(self, task_root):
        """界面用 ``fade_in``，渲染器用 ``FadeIn``。读模型说界面那一个。"""
        task_id, _ = _reviewed_task(task_root)
        scene_editor.set_effects(task_id, 0, transition="fade_in")
        described = scene_editor.describe(task_id)
        assert described["scenes"][0]["transition"] == "fade_in"
        assert described["scenes"][1]["transition"] == ""
        assert "fade_in" in described["transitions"]

    def test_an_empty_choice_means_follow_the_video(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        scene_editor.set_effects(task_id, 0, transition="fade_in")
        scene_editor.set_effects(task_id, 0, transition="")
        assert "transition" not in scene_manifest.load(task_id)["scenes"][0]

    def test_none_is_a_choice_and_not_the_absence_of_one(self, task_root):
        """显式的"这一刀不要转场"要存得下来，否则它和"没挑过"分不开。"""
        task_id, _ = _reviewed_task(task_root)
        scene_editor.set_effects(task_id, 0, transition="none")
        scene = scene_manifest.load(task_id)["scenes"][0]
        assert scene["transition"] == "none"
        assert scene_editor.describe(task_id)["scenes"][0]["transition"] == "none"

    def test_an_unknown_transition_is_refused_and_nothing_is_stored(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        with pytest.raises(scene_editor.SceneError) as error:
            scene_editor.set_effects(task_id, 0, transition="dissolve")
        assert "unknown transition" in str(error.value)
        assert "transition" not in scene_manifest.load(task_id)["scenes"][0]


class TestGettingItToTheRenderer:
    """挑过转场的任务必须开始写剪辑表，否则那段设置到不了渲染器。"""

    def test_a_chosen_transition_makes_the_task_render_from_an_edit_list(
        self, task_root
    ):
        task_id, _ = _reviewed_task(task_root)
        assert scene_manifest.edit_list_for_render(task_id) == []
        scene_editor.set_effects(task_id, 0, transition="fade_in")
        with patch.object(timeline, "probe_duration", return_value=5.0):
            scene_editor.prepare_render(task_id)
        edit_list = scene_manifest.edit_list_for_render(task_id)
        assert len(edit_list) == 2
        assert edit_list[0]["transition"] == VideoTransitionMode.fade_in.value
        assert "transition" not in edit_list[1]

    def test_touching_nothing_still_renders_the_old_way(self, task_root):
        """一样都没挑过时不写剪辑表，出片结果一个字节都不会变。"""
        task_id, _ = _reviewed_task(task_root)
        with patch.object(timeline, "probe_duration", return_value=5.0):
            scene_editor.prepare_render(task_id)
        assert scene_manifest.edit_list_for_render(task_id) == []

    def test_the_renderer_applies_the_scene_transition_over_the_video_one(self):
        """渲染器那一段的取舍：挑过就用它的，没挑过才用整条片子的。"""
        clips = [
            video_service.SubClippedVideoClip(
                file_path="a.mp4", start_time=0, end_time=1, transition="FadeIn"
            ),
            video_service.SubClippedVideoClip(
                file_path="b.mp4", start_time=0, end_time=1
            ),
            video_service.SubClippedVideoClip(
                file_path="c.mp4", start_time=0, end_time=1, transition="none"
            ),
        ]
        video_transition = VideoTransitionMode.slide_in.value

        def effective(item):
            chosen = getattr(item, "transition", None)
            if not chosen:
                return video_transition
            return None if chosen == "none" else chosen

        assert effective(clips[0]) == VideoTransitionMode.fade_in.value
        assert effective(clips[1]) == video_transition
        assert effective(clips[2]) is None


class TestChoosingTheMotion:
    """运镜是烘进素材的，所以改它要重烘一段。"""

    def test_choosing_a_motion_re_renders_the_clip_and_repoints_the_scene(
        self, task_root
    ):
        task_id, materials = _reviewed_task(task_root)
        baked = os.path.join(os.path.dirname(materials[0]), "re-baked.static-abc.mp4")
        with open(baked, "wb") as handle:
            handle.write(b"x")
        with patch.object(
            timeline, "ensure_image_motion", return_value=baked
        ) as rebake:
            scene_editor.set_effects(task_id, 0, image_motion="static")
        assert rebake.call_args.args[2] == "static"
        scene = scene_manifest.load(task_id)["scenes"][0]
        assert os.path.basename(scene["material"]) == "re-baked.static-abc.mp4"
        assert scene["image_motion"] == "static"

    def test_a_picture_without_its_source_image_says_so(self, task_root):
        """库存视频没有原图，重烘不了。要说清楚，而不是假装改了。"""
        task_id, _ = _reviewed_task(task_root)
        with patch.object(
            timeline,
            "ensure_image_motion",
            side_effect=timeline.TimelineError("its source image is no longer there"),
        ):
            with pytest.raises(scene_editor.SceneError) as error:
                scene_editor.set_effects(task_id, 0, image_motion="static")
        assert "source image" in str(error.value)
        assert "image_motion" not in scene_manifest.load(task_id)["scenes"][0]

    def test_an_unknown_motion_is_refused(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        with pytest.raises(scene_editor.SceneError) as error:
            scene_editor.set_effects(task_id, 0, image_motion="parallax")
        assert "unknown picture motion" in str(error.value)

    def test_a_clip_already_in_that_motion_is_not_re_rendered(self, task_root):
        """已经是这个运镜了就别再烘一遍：那是几秒钟的无谓编码。"""
        task_id, _ = _reviewed_task(task_root)
        with patch.object(timeline, "motion_of", return_value="static"):
            with patch.object(timeline, "ensure_image_motion") as rebake:
                scene_editor.set_effects(task_id, 0, image_motion="static")
        rebake.assert_not_called()
        assert scene_manifest.load(task_id)["scenes"][0]["image_motion"] == "static"


class TestReadingTheMotionBackOffDisk:
    """烘出来的片段把运镜写在文件名里，所以不必另外存一份。"""

    def test_the_motion_is_read_from_the_clip_name(self):
        name = "/t/" + "a" * 64 + ".static-0123456789abcdef.mp4"
        assert timeline.motion_of(name) == "static"
        assert timeline.motion_of("/t/" + "a" * 64 + ".zoom-0123.mp4") == "zoom"

    def test_a_downloaded_video_has_no_motion_in_its_name(self):
        assert timeline.motion_of("/t/vid-123.mp4") == ""
        assert timeline.motion_of("") == ""

    def test_stretching_a_still_clip_keeps_it_still(self, task_root):
        """以前这里会把"静止"悄悄换成"推近"：重渲时没把运镜带上。"""
        material = "/t/" + "a" * 64 + ".static-0123.mp4"
        with patch.object(timeline, "source_image_for", return_value="/t/pic.png"):
            with patch.object(
                video_service, "render_image_zoom_video", return_value="/t/out.mp4"
            ) as render:
                timeline.ensure_image_length("t", material, 9.0)
        assert render.call_args.kwargs["image_motion"] == "static"


class TestOverHttp:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from app import asgi

        return TestClient(asgi.app)

    def test_setting_a_transition_over_http(self, client, task_root):
        task_id, _ = _reviewed_task(task_root)
        response = client.put(
            f"/api/v1/tasks/{task_id}/scenes/0/effects",
            json={"transition": "zoom_in"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["transition"] == "zoom_in"

    def test_an_unknown_transition_is_a_400(self, client, task_root):
        task_id, _ = _reviewed_task(task_root)
        response = client.put(
            f"/api/v1/tasks/{task_id}/scenes/0/effects",
            json={"transition": "dissolve"},
        )
        assert response.status_code == 400
        assert "unknown transition" in response.text

    def test_the_choices_are_published_with_the_scene_list(self, client, task_root):
        """界面不写死这两张表：渲染器认的名字只有服务端知道。"""
        task_id, _ = _reviewed_task(task_root)
        data = client.get(f"/api/v1/tasks/{task_id}/scenes").json()["data"]
        assert "fade_in" in data["transitions"]
        assert data["image_motions"] == ["zoom", "static"]


class TestTheSoundOnTheCut:
    """每一刀响哪一声，由切过去的那一段说了算。"""

    def test_a_scene_remembers_its_cut_sound(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        with patch.object(
            scene_editor.transition_sound, "resolve_transition_sound",
            return_value="/sounds/whoosh.mp3",
        ):
            scene_editor.set_effects(task_id, 1, cut_sound="whoosh.mp3")
        assert scene_manifest.load(task_id)["scenes"][1]["cut_sound"] == "whoosh.mp3"
        assert scene_editor.describe(task_id)["scenes"][1]["cut_sound"] == "whoosh.mp3"

    def test_silent_is_a_choice_and_needs_no_file(self, task_root):
        """"这一刀不要响"不是一个文件名，不该去音效库里找它。"""
        task_id, _ = _reviewed_task(task_root)
        with patch.object(
            scene_editor.transition_sound, "resolve_transition_sound"
        ) as resolve:
            scene_editor.set_effects(task_id, 0, cut_sound="none")
        resolve.assert_not_called()
        assert scene_manifest.load(task_id)["scenes"][0]["cut_sound"] == "none"

    def test_a_sound_this_machine_does_not_have_is_refused_now(self, task_root):
        """现在就拒绝，而不是渲染完才发现片子里没响。"""
        task_id, _ = _reviewed_task(task_root)
        with patch.object(
            scene_editor.transition_sound, "resolve_transition_sound",
            side_effect=ValueError("no such sound"),
        ):
            with pytest.raises(scene_editor.SceneError) as error:
                scene_editor.set_effects(task_id, 0, cut_sound="missing.mp3")
        assert "unknown transition sound" in str(error.value)
        assert "cut_sound" not in scene_manifest.load(task_id)["scenes"][0]

    def test_choosing_a_sound_puts_the_task_on_the_edit_list_path(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        with patch.object(
            scene_editor.transition_sound, "resolve_transition_sound",
            return_value="/sounds/whoosh.mp3",
        ):
            scene_editor.set_effects(task_id, 1, cut_sound="whoosh.mp3")
        with patch.object(timeline, "probe_duration", return_value=5.0):
            scene_editor.prepare_render(task_id)
        edit_list = scene_manifest.edit_list_for_render(task_id)
        assert edit_list[1]["cut_sound"] == "whoosh.mp3"
        assert "cut_sound" not in edit_list[0]


class TestGroupingTheCutSounds:
    """渲染那一层：每一刀取哪一个文件，以及为什么按文件分组。"""

    def test_each_cut_takes_the_sound_of_the_scene_it_cuts_into(self):
        with patch.object(
            video_service.transition_sound_service, "resolve_transition_sound",
            side_effect=lambda name: f"/sounds/{name}",
        ):
            grouped = video_service._resolve_cut_sounds(
                [2.0, 4.0, 6.0], ["a.mp3", "", "b.mp3"], "default.mp3"
            )
        assert grouped == {
            "/sounds/a.mp3": [2.0],
            "/sounds/default.mp3": [4.0],
            "/sounds/b.mp3": [6.0],
        }

    def test_repeats_of_one_sound_are_grouped_so_it_is_decoded_once(self):
        with patch.object(
            video_service.transition_sound_service, "resolve_transition_sound",
            side_effect=lambda name: f"/sounds/{name}",
        ) as resolve:
            grouped = video_service._resolve_cut_sounds(
                [1.0, 2.0, 3.0], None, "whoosh.mp3"
            )
        assert grouped == {"/sounds/whoosh.mp3": [1.0, 2.0, 3.0]}
        assert resolve.call_count == 3

    def test_a_silent_scene_drops_only_its_own_cut(self):
        with patch.object(
            video_service.transition_sound_service, "resolve_transition_sound",
            side_effect=lambda name: f"/sounds/{name}",
        ):
            grouped = video_service._resolve_cut_sounds(
                [1.0, 2.0], ["none", ""], "whoosh.mp3"
            )
        assert grouped == {"/sounds/whoosh.mp3": [2.0]}

    def test_no_video_wide_sound_means_only_the_scenes_that_asked(self):
        with patch.object(
            video_service.transition_sound_service, "resolve_transition_sound",
            side_effect=lambda name: f"/sounds/{name}",
        ):
            grouped = video_service._resolve_cut_sounds([1.0, 2.0], ["", "b.mp3"], "")
        assert grouped == {"/sounds/b.mp3": [2.0]}

    def test_an_unresolvable_sound_drops_that_cut_and_not_the_render(self):
        """渲染已经走到最后一步，为一声 whoosh 丢掉整条片子不值得。"""
        def resolve(name):
            if name == "gone.mp3":
                raise ValueError("no such sound")
            return f"/sounds/{name}"

        with patch.object(
            video_service.transition_sound_service, "resolve_transition_sound",
            side_effect=resolve,
        ):
            grouped = video_service._resolve_cut_sounds(
                [1.0, 2.0], ["gone.mp3", "ok.mp3"], ""
            )
        assert grouped == {"/sounds/ok.mp3": [2.0]}


class TestBackgroundMusic:
    """整条片子的配乐。改的是这条任务自己的参数，不是全局设置。"""

    def test_reading_it_back_lists_what_is_available(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        with patch.object(
            scene_editor.bgm, "list_bgm_files",
            return_value=["/songs/Kurt - Cheel.mp3", "/songs/Coupe.mp3"],
        ):
            music = scene_editor.background_music(task_id)
        assert music["tracks"] == ["Kurt - Cheel.mp3", "Coupe.mp3"]
        assert music["external"] is False

    def test_choosing_a_track_writes_it_to_this_task_only(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        with (
            patch.object(
                scene_editor.bgm, "resolve_bgm_file",
                return_value="/songs/Kurt - Cheel.mp3",
            ),
            patch.object(
                scene_editor.bgm, "list_builtin_bgm_files",
                return_value=["/songs/Kurt - Cheel.mp3"],
            ),
            patch.object(scene_editor.bgm, "list_bgm_files", return_value=[]),
        ):
            scene_editor.set_background_music(task_id, track="Kurt - Cheel.mp3")
        params = scene_editor.load_params(task_id)
        assert params.bgm_file == "/songs/Kurt - Cheel.mp3"
        assert params.bgm_type == "preset"

    def test_an_uploaded_track_is_recorded_as_custom(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        with (
            patch.object(
                scene_editor.bgm, "resolve_bgm_file", return_value="/bgm/mine.mp3"
            ),
            patch.object(
                scene_editor.bgm, "list_builtin_bgm_files", return_value=[]
            ),
            patch.object(scene_editor.bgm, "list_bgm_files", return_value=[]),
        ):
            scene_editor.set_background_music(task_id, track="mine.mp3")
        assert scene_editor.load_params(task_id).bgm_type == "custom"

    def test_no_music_clears_both_the_type_and_the_file(self, task_root):
        """音量留着不动：用户下次选回来时不该从 0 开始。"""
        task_id, _ = _reviewed_task(task_root)
        with patch.object(scene_editor.bgm, "list_bgm_files", return_value=[]):
            music = scene_editor.set_background_music(task_id, track="")
        params = scene_editor.load_params(task_id)
        assert params.bgm_type == "" and params.bgm_file == ""
        assert music["track"] == ""

    def test_random_keeps_the_choice_for_render_time(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        with patch.object(scene_editor.bgm, "list_bgm_files", return_value=[]):
            music = scene_editor.set_background_music(task_id, track="random")
        params = scene_editor.load_params(task_id)
        assert params.bgm_type == "random" and params.bgm_file == ""
        assert music["track"] == "random"

    def test_a_track_this_machine_does_not_have_is_refused(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        with patch.object(
            scene_editor.bgm, "resolve_bgm_file", side_effect=ValueError("not allowed")
        ):
            with pytest.raises(scene_editor.SceneError) as error:
                scene_editor.set_background_music(task_id, track="../../secrets.toml")
        assert "unknown background music" in str(error.value)

    def test_the_volume_is_checked(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        with pytest.raises(scene_editor.SceneError) as error:
            scene_editor.set_background_music(task_id, volume=4)
        assert "between 0 and 1" in str(error.value)
        with pytest.raises(scene_editor.SceneError):
            scene_editor.set_background_music(task_id, volume=-0.5)

    def test_the_volume_alone_can_be_changed(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        with patch.object(scene_editor.bgm, "list_bgm_files", return_value=[]):
            scene_editor.set_background_music(task_id, volume=0.35)
        assert scene_editor.load_params(task_id).bgm_volume == 0.35

    def test_an_externally_scored_video_says_so(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        payload = scene_manifest.load_raw(task_id)
        payload["params"]["bgm_type"] = "sonilo"
        task_artifacts.patch_script_data(task_id, params=payload["params"])
        with patch.object(scene_editor.bgm, "list_bgm_files", return_value=[]):
            assert scene_editor.background_music(task_id)["external"] is True


class TestMusicOverHttp:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from app import asgi

        return TestClient(asgi.app)

    def test_reading_and_setting_over_http(self, client, task_root):
        task_id, _ = _reviewed_task(task_root)
        with patch.object(scene_editor.bgm, "list_bgm_files", return_value=[]):
            read = client.get(f"/api/v1/tasks/{task_id}/music")
        assert read.status_code == 200
        with (
            patch.object(
                scene_editor.bgm, "resolve_bgm_file", return_value="/songs/a.mp3"
            ),
            patch.object(
                scene_editor.bgm, "list_builtin_bgm_files",
                return_value=["/songs/a.mp3"],
            ),
            patch.object(scene_editor.bgm, "list_bgm_files", return_value=["/songs/a.mp3"]),
        ):
            response = client.put(
                f"/api/v1/tasks/{task_id}/music",
                json={"track": "a.mp3", "volume": 0.3},
            )
        assert response.status_code == 200
        assert response.json()["data"]["volume"] == 0.3

    def test_a_bad_volume_is_a_400(self, client, task_root):
        task_id, _ = _reviewed_task(task_root)
        response = client.put(
            f"/api/v1/tasks/{task_id}/music", json={"volume": 9}
        )
        assert response.status_code == 400

    def test_previewing_refuses_a_path_outside_the_library(self, client):
        """试听口子只认曲库那两个目录，不是任意文件下载。"""
        response = client.get("/api/v1/music/..%2F..%2Fconfig.toml")
        assert response.status_code in (400, 404)
