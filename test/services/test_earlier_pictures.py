# -*- coding: utf-8 -*-
"""把以前画过的某一张图换回来。

用户的原话："Would be great being able of selecting one of the images generated
in the project, imagine I have an image, it's good but not the best, I
regenerate the image but now it's terrible, how can I get back the old image
once generated? it's not possible now, we need this functionality"。

这件事的要害是：**那张图从来没丢。** 重生成只是把场景的 ``material`` 指向新
文件，旧文件一直躺在任务目录里（整个服务端没有一处删除场景素材的代码）。所以
需要的不是"版本管理"，是一个能看见那些文件、点回去的地方——而换回去只是改一
个文件名,几秒钟,不排队,不占显卡,不花钱。

两条路都要，而且理由不同：

* 场景上记下换掉的那几张（``history``）。顺序和来历只能记——目录本身答不出
  "这是第 4 段上一张"。
* 扫一遍任务目录（``picture_shelf``）。**老任务没有 history**，而用户此刻丢掉
  的那张正在一个老任务里。只靠记录的方案对他的问题毫无用处。
"""

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services import material, picture_shelf, scene_editor, scene_manifest

from test.services.test_scene_editor import (  # noqa: F401
    _material_file,
    _reviewed_task,
    idle_task,
    task_root,
)

#: 图片烘出来的片段就长这样：前 64 位是那张 png 的摘要，后面是运镜参数。
A_CLIP = ("a" * 64) + ".zoom-1111.mp4"
SAME_PICTURE_OTHER_MOVE = ("a" * 64) + ".zoom-2222.mp4"
ANOTHER_CLIP = ("b" * 64) + ".zoom-1111.mp4"


def _redraw(task_root, task_id, name, **kwargs):
    """重画第 0 段，新画面是 ``name``。返回那个文件的路径。"""
    replacement = _material_file(task_root, task_id, name)
    with patch.object(material, "download_videos", return_value=[replacement]):
        scene_editor.regenerate_material(task_id, 0, **kwargs)
    return replacement


class TestThePictureItReplacedIsStillThere:
    """整件事的前提。这一条要是不成立，上面那些设计全都不成立。"""

    def test_regenerating_does_not_delete_the_old_file(self, task_root):
        task_id, materials = _reviewed_task(task_root)
        _redraw(task_root, task_id, A_CLIP)
        assert os.path.isfile(materials[0]), (
            "重生成把旧文件删了的话，这个功能就只能靠真的存副本来做了"
        )


class TestWhatWeWriteDownWhenAPictureIsReplaced:
    def test_the_replaced_picture_is_remembered(self, task_root):
        task_id, materials = _reviewed_task(task_root)
        _redraw(task_root, task_id, A_CLIP)
        scene = scene_manifest.load(task_id)["scenes"][0]
        assert [one["material"] for one in scene["history"]] == [
            os.path.basename(materials[0])
        ]

    def test_it_survives_the_next_save(self, task_root):
        """``_normalise_scene`` 是个白名单，而它读和写都跑。

        不在那里留下 ``history``，这一条就会在下一次存盘时被静静抹掉——而
        下一次存盘可能只是用户改了一句台词。
        """
        task_id, materials = _reviewed_task(task_root)
        _redraw(task_root, task_id, A_CLIP)
        scene_editor.update_scene(task_id, 0, narration="something else")
        scene = scene_manifest.load(task_id)["scenes"][0]
        assert [one["material"] for one in scene["history"]] == [
            os.path.basename(materials[0])
        ]

    def test_the_prompt_that_drew_it_is_remembered_too(self, task_root):
        """"把上一张换回来"要连它当时的来历一起换回来，否则清单就在说谎。"""
        task_id, _ = _reviewed_task(task_root)
        _redraw(task_root, task_id, A_CLIP, keyword="a lighthouse at dusk")
        _redraw(task_root, task_id, ANOTHER_CLIP, keyword="a lighthouse at noon")
        history = scene_manifest.load(task_id)["scenes"][0]["history"]
        assert history[-1]["keyword"] == "a lighthouse at dusk"

    def test_a_file_that_did_not_change_is_not_remembered(self, task_root):
        """库存源的搜索带缓存，同一个关键词可能拿回同一个文件。

        把正在用的那一张也记成"旧的"，界面上就会多出一格假的历史。
        """
        task_id, materials = _reviewed_task(task_root, source="pexels")
        with patch.object(material, "download_videos", return_value=[materials[0]]):
            scene_editor.regenerate_material(task_id, 0)
        assert "history" not in scene_manifest.load(task_id)["scenes"][0]

    def test_a_scene_with_no_picture_has_nothing_to_remember(self):
        assert scene_manifest.remember_material({"material": ""}) is False

    def test_the_same_file_is_never_remembered_twice(self, task_root):
        """换回去再换走，那一张不该在历史里出现两遍——墙上也只会有一格。"""
        task_id, materials = _reviewed_task(task_root)
        first = os.path.basename(materials[0])
        _redraw(task_root, task_id, A_CLIP)
        scene_editor.restore_material(task_id, 0, first)
        _redraw(task_root, task_id, ANOTHER_CLIP)
        history = scene_manifest.load(task_id)["scenes"][0]["history"]
        assert [one["material"] for one in history].count(first) == 1

    def test_the_newest_record_of_a_file_is_the_one_that_is_kept(self):
        """同一张图可以被换下来好几次，每次的提示词都可能不同。

        留着第一条就会让"换回上一张"连着拿回一条过时的来历——图是对的，而
        旁边写着的是它上一次用的提示词。顺序也跟着最后那一次走：这个列表读
        起来是"它们各自是什么时候被换下来的"。
        """
        kept = scene_manifest._normalise_history([
            {"material": "a.mp4", "keyword": "first time"},
            {"material": "b.mp4", "keyword": "something else"},
            {"material": "a.mp4", "keyword": "second time"},
        ])
        assert [one["material"] for one in kept] == ["b.mp4", "a.mp4"]
        assert kept[-1]["keyword"] == "second time"

    def test_it_does_not_grow_without_end(self):
        """清单是要整份读写的 json，不能慢慢长成一个日志文件。"""
        scene = {"material": "start.mp4"}
        for at in range(scene_manifest.HISTORY_LIMIT + 8):
            scene["material"] = f"{at}.mp4"
            scene_manifest.remember_material(scene)
        assert len(scene["history"]) == scene_manifest.HISTORY_LIMIT
        # 丢掉的是最老的那几张，留下的是刚才那几张——用户要找的是"刚才那张"。
        assert scene["history"][-1]["material"] == f"{scene_manifest.HISTORY_LIMIT + 7}.mp4"

    def test_rubbish_in_an_old_manifest_does_not_break_reading_it(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        scenes = scene_manifest.load(task_id)["scenes"]
        scenes[0]["history"] = ["not a record", {}, {"material": "real.mp4"}, 7]
        scene_manifest.save_scenes(task_id, scenes)
        assert scene_manifest.load(task_id)["scenes"][0]["history"] == [
            {"material": "real.mp4"}
        ]


class TestEveryPictureThisVideoHasDrawn:
    """扫目录那一半。

    为什么它不能只读 ``history``：用户此刻丢掉的那张图在一个**老任务**里，
    那份清单里没有任何记录。目录里却有那个文件。
    """

    def test_the_ones_no_scene_uses_are_the_point(self, task_root):
        task_id, materials = _reviewed_task(task_root)
        _redraw(task_root, task_id, A_CLIP)
        spare = [
            one for one in picture_shelf.shelf(task_id) if one["in_use"] is None
        ]
        assert [one["material"] for one in spare] == [
            os.path.basename(materials[0])
        ]

    def test_it_finds_them_in_a_task_that_never_recorded_anything(self, task_root):
        """这一条就是用户今天能拿回那张图的理由。

        造一个老任务：目录里有被换下来的图，清单里一个 ``history`` 都没有。
        """
        task_id, materials = _reviewed_task(task_root)
        orphan = _material_file(task_root, task_id, ANOTHER_CLIP)
        assert all(
            "history" not in scene
            for scene in scene_manifest.load(task_id)["scenes"]
        )
        wall = picture_shelf.shelf(task_id)
        assert os.path.basename(orphan) in [one["material"] for one in wall]
        assert [one for one in wall if one["material"] == os.path.basename(orphan)][
            0
        ]["in_use"] is None

    def test_it_says_which_scene_is_using_each_one(self, task_root):
        task_id, materials = _reviewed_task(task_root)
        wall = {one["material"]: one for one in picture_shelf.shelf(task_id)}
        assert wall[os.path.basename(materials[0])]["in_use"] == 0
        assert wall[os.path.basename(materials[1])]["in_use"] == 1

    def test_the_rendered_video_is_not_one_of_the_pictures(self, task_root):
        """成片是这些图拼起来的结果，不是"画过的一张图"。"""
        task_id, _ = _reviewed_task(task_root)
        _material_file(task_root, task_id, "final-1.mp4")
        _material_file(task_root, task_id, "combined-1.mp4")
        names = [one["material"] for one in picture_shelf.shelf(task_id)]
        assert "final-1.mp4" not in names and "combined-1.mp4" not in names

    def test_the_still_behind_a_clip_is_not_a_second_tile(self, task_root):
        """本机出图落地一张 png，再烘成 mp4，上时间线的是后者。

        两个都列等于每张图在墙上出现两遍，而用户根本分不出哪一格该点。
        """
        task_id, _ = _reviewed_task(task_root)
        _redraw(task_root, task_id, A_CLIP)
        _material_file(task_root, task_id, "openai-image-abc123.png")
        names = [one["material"] for one in picture_shelf.shelf(task_id)]
        assert "openai-image-abc123.png" not in names

    def test_an_uploaded_picture_is_listed(self, task_root):
        """上传替换进来的 png 本身就是素材，不是中间产物。

        区别不在后缀上，在"有没有哪一段用过它"。
        """
        task_id, materials = _reviewed_task(task_root)
        names = [one["material"] for one in picture_shelf.shelf(task_id)]
        assert os.path.basename(materials[0]) in names

    def test_the_same_picture_with_two_camera_moves_is_one_tile(self, task_root):
        """同一张图换个运镜就是一个新文件，但它们是同一张画面。

        文件名前 64 位是那张 png 的摘要，所以这件事不用碰磁盘就能知道。
        """
        task_id, _ = _reviewed_task(task_root)
        _material_file(task_root, task_id, A_CLIP)
        _material_file(task_root, task_id, SAME_PICTURE_OTHER_MOVE)
        wall = picture_shelf.shelf(task_id)
        clips = [one for one in wall if picture_shelf.group_key(one["material"])
                 == "a" * 64]
        assert len(clips) == 1
        assert clips[0]["variants"] == 2

    def test_the_newest_is_first(self, task_root):
        """用户要找的几乎总是"刚才那张"。"""
        task_id, _ = _reviewed_task(task_root)
        old = Path(_material_file(task_root, task_id, A_CLIP))
        new = Path(_material_file(task_root, task_id, ANOTHER_CLIP))
        long_ago = time.time() - 9000
        os.utime(old, (long_ago, long_ago))
        wall = picture_shelf.shelf(task_id)
        assert wall[0]["material"] == new.name
        assert wall.index(
            [one for one in wall if one["material"] == new.name][0]
        ) < wall.index([one for one in wall if one["material"] == old.name][0])

    def test_a_task_with_no_folder_says_so(self, task_root):
        with pytest.raises(picture_shelf.ShelfError) as caught:
            picture_shelf.shelf("never-ran")
        assert "no folder" in str(caught.value)


class TestPuttingOneBack:
    def test_the_scene_points_at_it_again(self, task_root):
        task_id, materials = _reviewed_task(task_root)
        _redraw(task_root, task_id, A_CLIP)
        scene_editor.restore_material(task_id, 0, os.path.basename(materials[0]))
        assert scene_manifest.load(task_id)["scenes"][0]["material"] == materials[0]

    def test_nothing_is_redrawn(self, task_root):
        """这是整个功能最要紧的性质：那张图就在磁盘上。

        本机出一张图要几分钟并且占满显卡。换回上一张不该付这个代价——它只是
        改一个文件名。
        """
        task_id, materials = _reviewed_task(task_root)
        _redraw(task_root, task_id, A_CLIP)
        with patch.object(material, "download_videos") as download:
            scene_editor.restore_material(
                task_id, 0, os.path.basename(materials[0])
            )
        assert download.call_count == 0

    def test_the_one_it_replaced_is_kept_so_you_can_change_your_mind(
        self, task_root
    ):
        """看过两张之后改主意是常事，单向的"还原"只是把问题挪到另一边。"""
        task_id, materials = _reviewed_task(task_root)
        new = _redraw(task_root, task_id, A_CLIP)
        scene_editor.restore_material(task_id, 0, os.path.basename(materials[0]))
        history = scene_manifest.load(task_id)["scenes"][0]["history"]
        assert os.path.basename(new) in [one["material"] for one in history]
        # 而且真的能再换回去。
        scene_editor.restore_material(task_id, 0, os.path.basename(new))
        assert scene_manifest.load(task_id)["scenes"][0]["material"] == new

    def test_the_prompt_box_is_not_overwritten_but_the_old_prompt_is_handed_back(
        self, task_root
    ):
        """提示词是用户正在写的东西，不是一条记录。

        悄悄盖掉它就是把他刚敲进去的字改了；而完全不说，他也无从知道这张图
        当时是照什么画的。所以：不盖，但说出来。
        """
        task_id, _ = _reviewed_task(task_root)
        _redraw(task_root, task_id, A_CLIP, keyword="a lighthouse at dusk")
        _redraw(task_root, task_id, ANOTHER_CLIP, keyword="a lighthouse at noon")
        scene = scene_editor.restore_material(task_id, 0, A_CLIP)
        assert scene["keyword"] == "a lighthouse at noon"
        assert scene["restored_keyword"] == "a lighthouse at dusk"

    def test_the_style_and_the_photo_it_was_drawn_from_come_back_with_it(
        self, task_root
    ):
        """清单里这两个字段说的是**现在这张图**怎么来的（见 ``_normalise_scene``）。

        图换了而它们不换，那条记录就变成了假的。
        """
        task_id, _ = _reviewed_task(task_root)
        _redraw(task_root, task_id, A_CLIP, image_style="comic")
        scenes = scene_manifest.load(task_id)["scenes"]
        scenes[0]["reference"] = {
            "provider": "openverse",
            "page_url": "https://example.org/a-photo",
        }
        scene_manifest.save_scenes(task_id, scenes)
        _redraw(task_root, task_id, ANOTHER_CLIP, image_style="anime")

        after = scene_editor.restore_material(task_id, 0, A_CLIP)
        assert after["image_style"] == "comic"
        assert after["reference"]["page_url"] == "https://example.org/a-photo"

    def test_a_picture_drawn_from_no_photo_does_not_keep_the_other_one_s_credit(
        self, task_root
    ):
        """换回一张"凭空画的"图，上一张的照片来历必须跟着消失。"""
        task_id, materials = _reviewed_task(task_root)
        _redraw(task_root, task_id, A_CLIP)
        scenes = scene_manifest.load(task_id)["scenes"]
        scenes[0]["reference"] = {"provider": "openverse",
                                  "page_url": "https://example.org/x"}
        scene_manifest.save_scenes(task_id, scenes)
        after = scene_editor.restore_material(
            task_id, 0, os.path.basename(materials[0])
        )
        assert "reference" not in after

    def test_it_is_locked_so_a_later_batch_does_not_undo_the_choice(self, task_root):
        task_id, materials = _reviewed_task(task_root)
        _redraw(task_root, task_id, A_CLIP)
        after = scene_editor.restore_material(
            task_id, 0, os.path.basename(materials[0])
        )
        assert after["locked"] is True

    def test_only_this_video_s_own_pictures(self, task_root):
        """客户端递过来的是一个文件名，所以"它是不是这条片子的东西"由服务端判断。"""
        task_id, _ = _reviewed_task(task_root)
        _reviewed_task(task_root, task_id="elsewhere")
        other = _material_file(task_root, "elsewhere", "stolen.png")
        for name in ("stolen.png", other, "../elsewhere/stolen.png",
                     "nothing-like-this.mp4"):
            with pytest.raises(scene_editor.SceneError) as caught:
                scene_editor.restore_material(task_id, 0, name)
            assert "not a picture or video of this video" in str(caught.value)

    def test_putting_back_the_one_already_there_says_so(self, task_root):
        """静静成功会让用户以为换了一张，然后盯着没变的画面找原因。"""
        task_id, materials = _reviewed_task(task_root)
        with pytest.raises(scene_editor.SceneError) as caught:
            scene_editor.restore_material(
                task_id, 0, os.path.basename(materials[0])
            )
        assert "already using" in str(caught.value)

    def test_another_scene_s_picture_can_be_used_here_too(self, task_root):
        """用户的话是"selecting one of the images generated in the project"。

        第 2 段那张图正好适合第 1 段，这件事没有理由拦。
        """
        task_id, materials = _reviewed_task(task_root)
        scene_editor.restore_material(task_id, 0, os.path.basename(materials[1]))
        scenes = scene_manifest.load(task_id)["scenes"]
        assert scenes[0]["material"] == materials[1]
        # 第 2 段不动：两段指着同一个文件是允许的（同一张图用两次）。
        assert scenes[1]["material"] == materials[1]

    def test_it_refuses_while_the_task_is_busy(self, task_root):
        """渲染中、出图中的任务不能改画面。和别的编辑动作同一道闸。"""
        task_id, materials = _reviewed_task(task_root)
        _redraw(task_root, task_id, A_CLIP)
        with patch.object(
            scene_editor, "_require_idle",
            side_effect=scene_editor.SceneError("task review is busy"),
        ):
            with pytest.raises(scene_editor.SceneError) as caught:
                scene_editor.restore_material(
                    task_id, 0, os.path.basename(materials[0])
                )
        assert "busy" in str(caught.value)

    def test_the_cut_is_not_moved(self, task_root):
        """换一张图不该改这一段在片子里占多久——那是另一回事，用户没让动。"""
        task_id, materials = _reviewed_task(task_root)
        scenes = scene_manifest.load(task_id)["scenes"]
        scenes[0]["duration"] = 3.5
        scenes[0]["trim_start"] = 0.25
        scene_manifest.save_scenes(task_id, scenes)
        _redraw(task_root, task_id, A_CLIP)
        after = scene_editor.restore_material(
            task_id, 0, os.path.basename(materials[0])
        )
        assert after["duration"] == 3.5
        assert after["trim_start"] == 0.25


class TestTheEditorOffersIt:
    """界面那一半。

    为什么这几条钉的是源码里的字符串：这个编辑器没有构建步骤、没有测试运行
    器（一个 Python 仓库为一个页面引入 npm 工具链不值得），所以仓库里能自动
    检查的只有"那段代码还在不在、顺序对不对"。这类断言挡住的是真实发生过的
    回归：上一次改这个文件时，一段手写的循环被插进了另一个函数的回调里，
    语法完全合法，``node --check`` 和一条"它在文件里靠后"的断言都没发现。
    """

    @staticmethod
    def _editor() -> str:
        root = Path(__file__).resolve().parents[2]
        return (root / "resource" / "public" / "editor" / "editor.js").read_text(
            encoding="utf-8"
        )

    def test_the_way_in_sits_with_the_other_two_redraw_buttons(self):
        """用户来这块面板最常做的事就是重出，而重出一次就盖掉一张。

        "回到旧的"藏在别处的话，他只会再点一次重出去赌。
        """
        source = self._editor()
        assert "Earlier media…" in source
        assert source.index("Earlier media…") > source.index(
            '"Redraw from a photo"'
        )
        assert source.index("Earlier media…") < source.index(
            "const mine = pendingJobs(scene.index)"
        )

    def test_it_is_offered_for_stock_clips_too(self):
        """库存源的画面一样会被换掉，一样找不回来。

        那个按钮不能关在 ``isGenerated`` 里面——这一条就是钉这件事的。
        """
        source = self._editor()
        start = source.index('const earlier = button("Earlier media…"')
        between = source[source.index('"Redraw from a photo"'):start]
        # 中间必须有那个 if 块的收尾，否则按钮就在块里面。
        assert "\n  }\n" in between

    def test_the_grid_asks_the_server_which_pictures_exist(self):
        source = self._editor()
        assert "/pictures" in source

    def test_putting_one_back_does_not_go_through_the_regeneration_queue(self):
        """它不重画任何东西，所以不该排进那条等显卡的队。"""
        source = self._editor()
        start = source.index("function putPictureBack(")
        body = source[start:source.index("\n}", start)]
        assert "/restore" in body
        assert "material/queue" not in body

    def test_the_thumbnails_are_big_enough_to_recognise(self):
        """72 像素放大到一格的宽度全是糊的，而这一屏的全部意义就是认出画面。"""
        source = self._editor()
        assert "requestThumb({ url: tile.url, trim_start: 0 }, shot, 160)" in source
        # 尺寸必须进缓存键，否则轨道上那张 72 的会被当成这一张。
        assert "@${height}" in source

    def test_the_one_in_use_is_shown_but_not_clickable(self):
        """得先认出当前这张，才知道别的几张是哪几张。"""
        source = self._editor()
        start = source.index("function pictureTile(")
        body = source[start:source.index("\n}", start)]
        assert "On this scene now" in body
        assert "node.disabled = true" in body

    def test_the_unused_ones_come_first(self):
        source = self._editor()
        start = source.index("function drawShelf(")
        body = source[start:source.index("\n}", start)]
        assert "[...mine, ...free, ...elsewhere]" in body

    def test_a_folder_it_cannot_read_is_not_called_empty(self):
        """读不出来和"一张都没有"是两回事。

        说成后者，用户会以为那些图真的没了——而这个功能存在的理由正是它们
        其实还在。
        """
        source = self._editor()
        start = source.index("function drawShelf(")
        body = source[start:source.index("\n}", start)]
        assert "Could not read this video's folder" in body
        assert "no pictures or videos on disk yet" in body

    def test_the_old_prompt_is_offered_not_forced(self):
        source = self._editor()
        start = source.index("function putPictureBack(")
        body = source[start:source.index("\n}", start)]
        assert "restored_keyword" in body
        assert "the prompt box still has what you had" in body


class TestOverHttp:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from app import asgi

        return TestClient(asgi.app)

    def test_the_editor_can_list_the_pictures_and_put_one_back(
        self, client, task_root
    ):
        task_id, materials = _reviewed_task(task_root, task_id="http-pictures")
        _redraw(task_root, task_id, A_CLIP)

        listed = client.get(f"/api/v1/tasks/{task_id}/pictures")
        assert listed.status_code == 200, listed.text
        pictures = listed.json()["data"]["pictures"]
        spare = [one for one in pictures if one["in_use"] is None]
        assert [one["material"] for one in spare] == [
            os.path.basename(materials[0])
        ]
        # 地址能直接播：界面拿它抓缩略图，也拿它当预览。
        assert spare[0]["url"].endswith(
            f"/api/v1/stream/{task_id}/{os.path.basename(materials[0])}"
        )

        put = client.post(
            f"/api/v1/tasks/{task_id}/scenes/0/restore",
            json={"material": os.path.basename(materials[0])},
        )
        assert put.status_code == 200, put.text
        assert put.json()["data"]["material"] == os.path.basename(materials[0])
        assert scene_manifest.load(task_id)["scenes"][0]["material"] == materials[0]

    def test_a_file_that_is_not_this_video_s_is_refused_with_a_reason(
        self, client, task_root
    ):
        task_id, _ = _reviewed_task(task_root, task_id="http-refuse")
        answer = client.post(
            f"/api/v1/tasks/{task_id}/scenes/0/restore",
            json={"material": "../../../../Windows/System32/calc.exe"},
        )
        assert answer.status_code >= 400
        assert "not a picture or video of this video" in answer.text
