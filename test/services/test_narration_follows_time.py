"""旁白跟着时间走，不跟着画面走。

Why these tests exist
---------------------
一条片子只有一条配音，字幕是按那条配音的时间轴生成的。所以"这张图上会念什么"
只由这张图在成片里的起止时间决定——它是算得出来的，不是存在图上的属性。

原来的实现把台词存在场景记录上，并且只在第一次打开审片界面时按时间配一次。
之后用户把第 7 张图拖到最前面，那句话跟着图走了：

* 界面在这张图下面显示另一段的台词，用户以为渲染器配错了图；
* 而"重新配音"读的就是这份列表，按一下整条旁白就按画面顺序重念一遍——
  一次静悄悄的、把片子念乱的改动。

成片本身一直是好的（渲染器只从场景拿素材路径，字幕走 ``subtitle.srt``），
坏的是界面和重新配音。这些测试钉住的是修好之后的性质：画面换位置，词留在
原处；每一段显示的就是它在屏幕上时真正会念到的那几句；而用户亲手写下的字
谁也不许盖掉。
"""

import os
from unittest.mock import patch

import pytest

from app.models.schema import VideoParams
from app.services import scene_editor, scene_manifest, task_artifacts, timeline
from app.utils import utils

from test.services.test_scene_editor import idle_task, task_root  # noqa: F401

SCRIPT = "One. Two. Three. Four."

SUBTITLE = """1
00:00:00,000 --> 00:00:02,000
One.

2
00:00:02,000 --> 00:00:04,000
Two.

3
00:00:04,000 --> 00:00:06,000
Three.

4
00:00:06,000 --> 00:00:08,000
Four.
"""


def _narrated_task(task_root, task_id="spoken", scenes=2, subtitles=True):
    """四句旁白、八秒配音、``scenes`` 张图的待审任务。

    句数故意多于图数：关键词个数决定图数，句数由脚本决定，两者几乎从不相等,
    而正是"一张图上念好几句"这种情况让按下标配对彻底失真。
    """
    directory = task_root / task_id
    directory.mkdir(parents=True, exist_ok=True)

    def written(name: str, data: bytes = b"x") -> str:
        target = directory / name
        target.write_bytes(data)
        return str(target)

    params = VideoParams(
        video_subject="seeds",
        video_script=SCRIPT,
        video_source="openai_image",
        video_clip_duration=5,
    )
    task_artifacts.write_script_data(
        task_id,
        {
            "script": SCRIPT,
            "search_terms": ["a", "b"],
            "params": params.model_dump(),
        },
    )
    materials = [written(f"{index + 1}.png") for index in range(scenes)]
    scene_manifest.record_prepared_artifacts(
        task_id,
        video_script=SCRIPT,
        video_terms=["a", "b"],
        materials=materials,
        audio_file=written("audio.mp3"),
        audio_duration=8.0,
        subtitle_path=(
            written("subtitle.srt", SUBTITLE.encode("utf-8")) if subtitles else ""
        ),
        video_source="openai_image",
        clip_duration=5,
    )
    return task_id, materials


def spoken(task_id: str) -> list[str]:
    """每一段画面上显示的台词，按场景顺序。"""
    return [scene["narration"] for scene in scene_editor.describe(task_id)["scenes"]]


class TestWhatEachPictureSays:
    def test_the_lines_come_from_the_subtitle_timing(self, task_root):
        """两张图平分八秒，所以前两句归第一张，后两句归第二张。

        按下标配对的话第二张图会显示 "Two."，而它在屏幕上时念的是三四句。
        """
        task_id, _ = _narrated_task(task_root)
        assert spoken(task_id) == ["One.\nTwo.", "Three.\nFour."]

    def test_every_line_is_shown_somewhere(self, task_root):
        """一句都不许丢。按下标配对时 12 张图配 28 句会丢掉 16 句。"""
        task_id, _ = _narrated_task(task_root)
        shown = "\n".join(spoken(task_id))
        for line in ("One.", "Two.", "Three.", "Four."):
            assert line in shown

    def test_deleting_a_picture_respreads_the_words(self, task_root):
        """删掉一张图，剩下那张铺满整条旁白，于是四句都归它。

        删除不该让那几句消失：配音没变，那些话照样会被念出来。
        """
        task_id, _ = _narrated_task(task_root)
        scene_editor.delete_scene(task_id, 0)
        assert spoken(task_id) == ["One.\nTwo.\nThree.\nFour."]

    def test_holding_a_picture_longer_gives_it_more_lines(self, task_root):
        """第一张停久一点，它就盖住更多句子。时间决定台词，这是同一件事。"""
        task_id, _ = _narrated_task(task_root)
        with patch.object(timeline, "probe_duration", return_value=20.0):
            scene_editor.update_timing(task_id, 0, duration=7.0)
        assert "Three." in spoken(task_id)[0]

    def test_without_subtitles_the_lines_are_still_spread(self, task_root):
        """关掉字幕的任务没有时间点，按句数均分——仍然好过按下标丢一半。

        句末的句号在这里没有了：没有字幕可读时台词是 ``split_narration`` 从
        脚本里切出来的句子，而不是字幕文件里的原文。
        """
        task_id, _ = _narrated_task(task_root, subtitles=False)
        assert spoken(task_id) == ["One\nTwo", "Three\nFour"]


class TestWordsStayWhenPicturesMove:
    def test_reordering_leaves_the_words_where_they_were(self, task_root):
        """图换了位置，第 0 到 4 秒念的还是前两句。"""
        task_id, _ = _narrated_task(task_root)
        before = spoken(task_id)
        scene_editor.reorder_scenes(task_id, [1, 0])
        assert spoken(task_id) == before

    def test_reordering_moves_the_pictures(self, task_root):
        """词不动，但图真的动了——否则这条测试什么都没证明。"""
        task_id, materials = _narrated_task(task_root)
        scene_editor.reorder_scenes(task_id, [1, 0])
        assert scene_manifest.materials_for_render(task_id) == [
            os.path.realpath(materials[1]),
            os.path.realpath(materials[0]),
        ]

    def test_re_recording_after_a_reorder_keeps_the_script_in_order(self, task_root):
        """这是那个静悄悄把片子念乱的 bug。

        重排之后按"重新配音"，旧实现把场景台词按**画面顺序**拼成脚本，于是
        整条旁白从 "One. Two. Three. Four." 变成 "Three. Four. One. Two."，
        而界面上没有任何提示。
        """
        task_id, _ = _narrated_task(task_root)
        scene_editor.reorder_scenes(task_id, [1, 0])
        params = scene_editor.prepare_audio_rerun(task_id)
        assert params.video_script == "One.\nTwo.\nThree.\nFour."
        assert scene_manifest.load(task_id)["script"] == params.video_script

    def test_reordering_still_does_not_invalidate_the_recording(self, task_root):
        """重排不要求重新配音：顺序没变的那条配音依然对得上字幕。"""
        task_id, _ = _narrated_task(task_root)
        scene_editor.reorder_scenes(task_id, [1, 0])
        assert scene_manifest.load(task_id)["audio_stale"] is False


class TestHandEdits:
    def test_a_rewritten_line_is_kept(self, task_root):
        task_id, _ = _narrated_task(task_root)
        scene_editor.update_scene(task_id, 0, narration="A brand new opening.")
        assert spoken(task_id)[0] == "A brand new opening."
        assert scene_manifest.load(task_id)["audio_stale"] is True

    def test_a_rewritten_line_reaches_the_re_recorded_script(self, task_root):
        task_id, _ = _narrated_task(task_root)
        scene_editor.update_scene(task_id, 0, narration="A brand new opening.")
        params = scene_editor.prepare_audio_rerun(task_id)
        # 改过台词之后那份字幕就不作数了（它是旧词的时间轴），所以剩下两句
        # 来自脚本本身，句号被切句切掉了。
        assert params.video_script == "A brand new opening.\nThree\nFour"

    def test_a_rewritten_line_survives_a_reorder_and_stays_put(self, task_root):
        """手写的字属于那段时间，和算出来的台词一样不跟着图走。"""
        task_id, _ = _narrated_task(task_root)
        scene_editor.update_scene(task_id, 0, narration="A brand new opening.")
        scene_editor.reorder_scenes(task_id, [1, 0])
        assert spoken(task_id)[0] == "A brand new opening."

    def test_a_rewritten_line_is_not_recomputed_away(self, task_root):
        """改完之后时间线又动了，用户写的字还在。"""
        task_id, _ = _narrated_task(task_root)
        scene_editor.update_scene(task_id, 1, narration="My own ending.")
        with patch.object(timeline, "probe_duration", return_value=20.0):
            scene_editor.update_timing(task_id, 0, duration=7.0)
        assert spoken(task_id)[1] == "My own ending."

    def test_clearing_a_line_drops_it_from_the_script(self, task_root):
        """清空是"重新配音时别念这几句"，不是"交回去重新算一遍"。"""
        task_id, _ = _narrated_task(task_root)
        scene_editor.update_scene(task_id, 0, narration="")
        assert spoken(task_id)[0] == ""
        params = scene_editor.prepare_audio_rerun(task_id)
        assert params.video_script == "Three\nFour"

    def test_saving_the_text_that_is_already_shown_changes_nothing(self, task_root):
        """按一下"保存文字"而什么都没改，不该作废那条配音。

        界面上显示的是算出来的台词，而存在场景上的往往是空字符串。拿提交的
        文字和**存着的**比，用户只要碰一下保存就会被当成改了台词，然后被要求
        重新配音——一次几分钟的 TTS，换来一模一样的词。
        """
        task_id, _ = _narrated_task(task_root)
        shown = spoken(task_id)[0]
        scene_editor.update_scene(task_id, 0, narration=shown)
        assert scene_manifest.load(task_id)["audio_stale"] is False
        assert spoken(task_id)[0] == shown


class TestRewritingTheWholeScript:
    def test_the_new_words_are_shown_and_the_recording_goes_stale(self, task_root):
        task_id, _ = _narrated_task(task_root)
        scene_editor.rewrite_script(task_id, "Alpha. Beta. Gamma. Delta.")
        assert scene_manifest.load(task_id)["audio_stale"] is True
        shown = "\n".join(spoken(task_id))
        assert "Alpha" in shown and "Delta" in shown
        # 旧词不许还留在界面上：那份字幕是按旧词的时间轴生成的，改完脚本
        # 之后它只会显示改之前的样子。
        assert "One" not in shown

    def test_the_new_script_is_what_gets_re_recorded(self, task_root):
        task_id, _ = _narrated_task(task_root)
        scene_editor.rewrite_script(task_id, "Alpha. Beta.")
        params = scene_editor.prepare_audio_rerun(task_id)
        assert "Alpha" in params.video_script
        assert "One" not in params.video_script

    def test_rewriting_clears_the_per_scene_overrides(self, task_root):
        """脚本本身就是台词了，旧的局部修改不该再挡着新写下的句子。"""
        task_id, _ = _narrated_task(task_root)
        scene_editor.update_scene(task_id, 0, narration="An override.")
        scene_editor.rewrite_script(task_id, "Alpha. Beta. Gamma. Delta.")
        assert "An override." not in "\n".join(spoken(task_id))

    def test_an_empty_script_is_refused(self, task_root):
        task_id, _ = _narrated_task(task_root)
        with pytest.raises(scene_editor.SceneError) as error:
            scene_editor.rewrite_script(task_id, "   ")
        assert "cannot be empty" in str(error.value)
        assert scene_manifest.load(task_id)["script"] == SCRIPT

    def test_saving_the_same_script_does_not_invalidate_the_recording(self, task_root):
        task_id, _ = _narrated_task(task_root)
        scene_editor.rewrite_script(task_id, SCRIPT)
        assert scene_manifest.load(task_id)["audio_stale"] is False


class TestOverHttp:
    """接口上的那两条路：读回来的台词，和整条脚本的改写。"""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from app import asgi

        return TestClient(asgi.app)

    def test_the_listed_scenes_carry_the_spoken_lines(self, client, task_root):
        task_id, _ = _narrated_task(task_root)
        listed = client.get(f"/api/v1/tasks/{task_id}/scenes").json()["data"]
        assert [scene["narration"] for scene in listed["scenes"]] == [
            "One.\nTwo.",
            "Three.\nFour.",
        ]
        assert listed["scenes"][0]["narration_edited"] is False

    def test_rewriting_the_script_over_http(self, client, task_root):
        task_id, _ = _narrated_task(task_root)
        response = client.put(
            f"/api/v1/tasks/{task_id}/script",
            json={"script": "Alpha. Beta. Gamma. Delta."},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["audio_stale"] is True
        assert "Alpha" in "\n".join(
            scene["narration"] for scene in data["scenes"]
        )

    def test_an_empty_script_is_refused_over_http(self, client, task_root):
        task_id, _ = _narrated_task(task_root)
        response = client.put(f"/api/v1/tasks/{task_id}/script", json={"script": ""})
        assert response.status_code == 400
        assert "cannot be empty" in response.text
        assert scene_manifest.load(task_id)["script"] == SCRIPT

    def test_a_hand_edit_is_reported_as_one(self, client, task_root):
        task_id, _ = _narrated_task(task_root)
        client.put(
            f"/api/v1/tasks/{task_id}/scenes/0",
            json={"narration": "My own words."},
        )
        listed = client.get(f"/api/v1/tasks/{task_id}/scenes").json()["data"]
        assert listed["scenes"][0]["narration"] == "My own words."
        assert listed["scenes"][0]["narration_edited"] is True
