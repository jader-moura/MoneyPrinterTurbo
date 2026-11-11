"""素材阶段半路断掉的任务要能接着跑完，不重新买已经出完的画面。

Why these tests exist
---------------------
一条任务跑到第 29 张图时连接被掐断（``ConnectionReset`` → "unconfirmed image
request (no retry to avoid double billing)"）。那 29 张已经付过钱、已经渲成
mp4、已经写进 ``material_sources``——但恢复渲染需要的东西（配音、字幕、场景
顺序）当时只在素材阶段**全部成功之后**才一次性写进清单。于是：

* 清单里没有场景，界面上这条任务是 "0 scenes"，点开一片空白；
* ``start_from="materials"`` 也跑不起来，因为清单里没有配音；
* 唯一的出路是从脚本整条重来，把那 29 张重新买一遍，而且买到的是另外 29 张
  不一样的图。

这里钉住三件事：配音和字幕在素材阶段之前就落盘；已经渲好的片段在重跑时被
认出来接着用；认人靠文件名，所以时长或运镜一改就整批作废重新生成。
"""

import os
from unittest.mock import patch

import pytest

from app.models.schema import ImageMotion, MaterialInfo, VideoAspect
from app.services import material, task_artifacts, video


def _clip(directory, name, hold=3, motion=ImageMotion.zoom):
    """一个"上一轮已经渲好的片段"：名字按内容寻址那套规则拼。"""
    path = os.path.join(directory, name + video.image_clip_suffix(hold, motion))
    with open(path, "wb") as handle:
        handle.write(b"not really an mp4")
    return os.path.basename(path)


def _record(local_file, term="a term", hold=3):
    return {
        "provider": "openai_image",
        "local_file": local_file,
        "duration": hold,
        "search_term": term,
    }


class TestRecognisingWhatWasAlreadyPaidFor:
    """``_clips_already_paid_for``：按文件名认上一轮的片段。"""

    def _found(self, tmp_path, records, hold=3, motion=ImageMotion.zoom):
        with patch.object(
            task_artifacts, "read_script_data",
            return_value={"material_sources": records},
        ):
            with patch.object(video, "rendered_clip_is_usable", lambda path: os.path.isfile(path)):
                return material._clips_already_paid_for(
                    "t1", str(tmp_path), "openai_image", hold, motion
                )

    def test_the_clips_from_the_earlier_run_are_found(self, tmp_path):
        names = [_clip(tmp_path, f"{index:064d}") for index in range(3)]
        paths, sources, total = self._found(
            tmp_path, [_record(name) for name in names]
        )
        assert [os.path.basename(path) for path in paths] == names
        assert len(sources) == 3
        assert total == pytest.approx(9.0)

    def test_a_clip_whose_file_is_gone_is_not_counted(self, tmp_path):
        kept = _clip(tmp_path, "a" * 64)
        missing = "b" * 64 + video.image_clip_suffix(3, ImageMotion.zoom)
        paths, _, total = self._found(
            tmp_path, [_record(kept), _record(missing)]
        )
        assert [os.path.basename(path) for path in paths] == [kept]
        assert total == pytest.approx(3.0)

    def test_a_different_hold_invalidates_the_whole_batch(self, tmp_path):
        """每段停留时间一改，那批片段拼出来的节奏就不是用户要的了。"""
        names = [_clip(tmp_path, f"{index:064d}", hold=3) for index in range(3)]
        paths, _, total = self._found(
            tmp_path, [_record(name) for name in names], hold=5
        )
        assert paths == []
        assert total == 0.0

    def test_a_different_motion_invalidates_the_whole_batch(self, tmp_path):
        names = [
            _clip(tmp_path, f"{index:064d}", motion=ImageMotion.zoom)
            for index in range(2)
        ]
        paths, _, _ = self._found(
            tmp_path, [_record(name) for name in names], motion=ImageMotion.static
        )
        assert paths == []

    def test_another_provider_s_records_are_left_alone(self, tmp_path):
        name = _clip(tmp_path, "c" * 64)
        record = _record(name)
        record["provider"] = "pexels"
        paths, _, _ = self._found(tmp_path, [record])
        assert paths == []

    def test_a_task_with_no_records_starts_from_the_first_keyword(self, tmp_path):
        assert self._found(tmp_path, []) == ([], [], 0.0)

    def test_an_unreadable_manifest_does_not_raise(self, tmp_path):
        with patch.object(
            task_artifacts, "read_script_data", side_effect=OSError("disk gone")
        ):
            assert material._clips_already_paid_for(
                "t1", str(tmp_path), "openai_image", 3, ImageMotion.zoom
            ) == ([], [], 0.0)


class TestResumingTheGeneration:
    """端到端：接着跑只买缺的那几张。"""

    def _run(self, tmp_path, terms, audio_duration, hold, records):
        generated = []

        def fake_generator(*, search_term, minimum_duration, **_kwargs):
            generated.append(search_term)
            return [
                MaterialInfo(
                    provider="openai_image",
                    url=f"{tmp_path}/new-{len(generated)}.png",
                    duration=minimum_duration,
                )
            ]

        with patch.object(
            task_artifacts, "read_script_data",
            return_value={"material_sources": records},
        ), patch.object(
            video, "rendered_clip_is_usable", lambda path: os.path.isfile(path)
        ), patch.object(
            material, "_render_openai_image_video",
            side_effect=lambda url, hold_, **_k: f"{url}.mp4",
        ), patch.object(material, "_persist_material_sources") as persist:
            paths = material._download_videos_openai_image_on_demand(
                task_id="t1",
                search_terms=terms,
                video_aspect=VideoAspect.portrait,
                audio_duration=audio_duration,
                max_clip_duration=hold,
                material_directory=str(tmp_path),
                image_generator=fake_generator,
                continue_unfinished=True,
            )
        return paths, generated, persist

    def test_only_the_missing_pictures_are_generated(self, tmp_path):
        """旁白 30 秒、每段 3 秒要 10 张；已经有 7 张,只买 3 张。"""
        names = [_clip(tmp_path, f"{index:064d}") for index in range(7)]
        paths, generated, _ = self._run(
            tmp_path, ["a", "b", "c"], 30.0, 3, [_record(n) for n in names]
        )
        assert len(generated) == 3
        assert len(paths) == 10
        assert [os.path.basename(path) for path in paths[:7]] == names

    def test_the_resumed_run_picks_the_keywords_up_where_it_stopped(self, tmp_path):
        """一张图一个关键词，所以第 8 张用的是第 8 个关键词。"""
        names = [_clip(tmp_path, f"{index:064d}") for index in range(7)]
        _, generated, _ = self._run(
            tmp_path, ["a", "b", "c"], 30.0, 3, [_record(n) for n in names]
        )
        # ["a","b","c"] 轮播 10 轮：a b c a b c a | b c a
        assert generated == ["b", "c", "a"]

    def test_nothing_is_generated_when_the_pictures_already_cover_it(self, tmp_path):
        """画面其实已经够了,只是清单没写成——这一趟一张都不该买。"""
        names = [_clip(tmp_path, f"{index:064d}") for index in range(10)]
        paths, generated, persist = self._run(
            tmp_path, ["a"], 30.0, 3, [_record(n) for n in names]
        )
        assert generated == []
        assert len(paths) == 10
        persist.assert_called()

    def test_regenerating_one_picture_never_picks_up_an_old_one(self, tmp_path):
        """编辑器里重生成单张图：改了提示词就必须重画,不能把旧图找回来。

        用户把提示词改成 "Thousand of clocks running late",拿回来的却是片子
        里已经在用的那张微波炉——因为重生成走的是同一个下载函数,而它那一趟
        只要一段片长,任务目录里随便哪张旧图都"够"了。
        """
        names = [_clip(tmp_path, f"{index:064d}") for index in range(29)]
        records = [_record(name) for name in names]
        generated = []

        def fake_generator(*, search_term, minimum_duration, **_kwargs):
            generated.append(search_term)
            return [
                MaterialInfo(
                    provider="openai_image",
                    url=f"{tmp_path}/fresh.png",
                    duration=minimum_duration,
                )
            ]

        with patch.object(
            task_artifacts, "read_script_data",
            return_value={"material_sources": records},
        ), patch.object(
            video, "rendered_clip_is_usable", lambda path: os.path.isfile(path)
        ), patch.object(
            material, "_render_openai_image_video",
            side_effect=lambda url, hold_, **_k: f"{url}.mp4",
        ), patch.object(material, "_persist_material_sources"):
            # 重生成那一趟的形状：只要一段片长。
            paths = material._download_videos_openai_image_on_demand(
                task_id="t1",
                search_terms=["Thousand of clocks running late"],
                video_aspect=VideoAspect.portrait,
                audio_duration=3.0,
                max_clip_duration=3,
                material_directory=str(tmp_path),
                image_generator=fake_generator,
            )
        assert generated == ["Thousand of clocks running late"]
        assert paths == [f"{tmp_path}/fresh.png.mp4"]
        assert not any(name in paths[0] for name in names)

    def test_a_first_run_is_unchanged(self, tmp_path):
        """没有上一轮时行为和以前一个字节都不差。"""
        paths, generated, _ = self._run(tmp_path, ["a", "b"], 12.0, 3, [])
        assert len(generated) == 4
        assert len(paths) == 4
        assert generated == ["a", "b", "a", "b"]

    def test_the_earlier_records_are_kept_in_the_manifest(self, tmp_path):
        """重跑不能把上一轮那几条来源记录顶掉,否则下一次重跑又认不出来了。"""
        names = [_clip(tmp_path, f"{index:064d}") for index in range(7)]
        _, _, persist = self._run(
            tmp_path, ["a"], 30.0, 3, [_record(n) for n in names]
        )
        written = persist.call_args[0][1]
        assert [item["local_file"] for item in written[:7]] == names
        assert len(written) == 10
