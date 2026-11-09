"""要生成多少张画面，由旁白长度和单段时长决定，不由关键词条数决定。

Why these tests exist
---------------------
``_download_videos_openai_image_on_demand`` 原来是 ``for term in search_terms``：
一个关键词一张图，关键词用完就收工。它内部一直在累计 ``total_duration`` 并
和 ``required_duration`` 比较，但那个比较只用来"够了就提前停",没有"不够就
继续"的那一半。

于是 12 个关键词、每段 2 秒只能铺出 24 秒，而旁白有 72 秒。缺口留给渲染器
循环播放，用户看到的是"第 12 段之后整部片子都在重播前面的画面"。

同一个关键词再生成一次得到的是另一张图（随机种子不同），所以补出来的是新
画面，不是同一张图播两遍。
"""

from unittest.mock import patch

import pytest

from app.models.schema import MaterialInfo, VideoAspect
from app.services import material, timeline


class TestTermsToCover:
    """关键词轮播的条数上限就是"铺满旁白所需的张数"。"""

    def test_one_round_is_enough_when_the_keywords_already_cover_it(self):
        terms = ["a", "b", "c"]
        assert list(material._terms_to_cover(terms, 4.0, 2)) == ["a", "b", "c"]

    def test_keywords_cycle_until_the_narration_is_covered(self):
        """3 个关键词、每段 2 秒、旁白 72 秒：需要 36 张。"""
        produced = list(material._terms_to_cover(["a", "b", "c"], 72.0, 2))
        assert len(produced) == 36
        assert produced[:4] == ["a", "b", "c", "a"]

    def test_a_shorter_hold_needs_more_pictures(self):
        assert len(list(material._terms_to_cover(["a"], 30.0, 2))) == 15
        assert len(list(material._terms_to_cover(["a"], 30.0, 5))) == 6

    def test_never_fewer_than_the_keywords_the_model_returned(self):
        """旁白很短时也不能少于关键词条数，否则会凭空丢掉用户填的关键词。"""
        assert len(list(material._terms_to_cover(["a", "b", "c"], 1.0, 5))) == 3

    def test_no_keywords_produces_nothing(self):
        assert list(material._terms_to_cover([], 72.0, 2)) == []
        assert list(material._terms_to_cover(["", "  "], 72.0, 2)) == []

    def test_an_unusable_hold_does_not_divide_by_zero(self):
        assert len(list(material._terms_to_cover(["a"], 10.0, 0))) == 10


class TestGeneratedPictureCount:
    """端到端：按需生成的张数要铺满旁白，并且够了就立刻停。"""

    def _run(self, tmp_path, terms, audio_duration, hold):
        generated = []

        def fake_generator(*, search_term, minimum_duration, **_kwargs):
            generated.append(search_term)
            return [
                MaterialInfo(
                    provider="openai_image",
                    url=f"{tmp_path}/{len(generated)}.png",
                    duration=minimum_duration,
                )
            ]

        with patch.object(material, "_render_openai_image_video") as render:
            render.side_effect = lambda url, hold_, **_k: f"{url}.mp4"
            with patch.object(material, "_persist_material_sources"):
                paths = material._download_videos_openai_image_on_demand(
                    task_id="t1",
                    search_terms=terms,
                    video_aspect=VideoAspect.portrait,
                    audio_duration=audio_duration,
                    max_clip_duration=hold,
                    material_directory=str(tmp_path),
                    image_generator=fake_generator,
                )
        return paths, generated

    def test_enough_pictures_are_generated_to_cover_the_narration(self, tmp_path):
        """12 个关键词、每段 2 秒、旁白 72 秒：要 36 张，不是 12 张。"""
        terms = [f"term {index}" for index in range(12)]
        paths, generated = self._run(tmp_path, terms, 72.0, 2)
        assert len(paths) == 36
        assert len(generated) == 36

    def test_the_user_example_fifteen_scenes_for_thirty_seconds(self, tmp_path):
        """30 秒旁白、每段 2 秒，至少 15 段画面。"""
        paths, _ = self._run(tmp_path, ["eye"], 30.0, 2)
        assert len(paths) >= 15

    def test_generation_still_stops_as_soon_as_the_narration_is_covered(
        self, tmp_path
    ):
        """付费安全语义不变：够了就停，不会为用不到的画面多付一次。"""
        terms = [f"term {index}" for index in range(40)]
        paths, generated = self._run(tmp_path, terms, 10.0, 5)
        assert len(paths) == 2
        assert len(generated) == 2

    def test_keywords_are_reused_in_order(self, tmp_path):
        paths, generated = self._run(tmp_path, ["a", "b"], 8.0, 2)
        assert generated == ["a", "b", "a", "b"]
        assert len(set(paths)) == len(paths)


class TestCycleWindows:
    """轮播后的真实时间线：旁白要按它来分配，而不是只按第一轮。"""

    windows = [
        {"start": 0.0, "end": 2.0},
        {"start": 2.0, "end": 4.0},
        {"start": 4.0, "end": 6.0},
    ]

    def test_one_pass_when_the_pictures_already_cover_the_narration(self):
        cycled = timeline.cycle_windows(self.windows, 6.0)
        assert len(cycled) == 3
        assert [item["scene"] for item in cycled] == [0, 1, 2]

    def test_the_windows_repeat_until_the_narration_is_covered(self):
        cycled = timeline.cycle_windows(self.windows, 18.0)
        assert len(cycled) == 9
        assert [item["scene"] for item in cycled] == [0, 1, 2] * 3
        assert cycled[-1]["end"] == pytest.approx(18.0)

    def test_the_repeated_windows_run_continuously(self):
        cycled = timeline.cycle_windows(self.windows, 12.0)
        for earlier, later in zip(cycled, cycled[1:]):
            assert later["start"] == pytest.approx(earlier["end"])

    def test_without_a_target_a_single_pass_is_returned(self):
        assert len(timeline.cycle_windows(self.windows)) == 3

    def test_no_windows_cycle_to_nothing(self):
        assert timeline.cycle_windows([], 30.0) == []

    def test_zero_length_windows_do_not_spin_forever(self):
        """算出 0 长度的时间段不能让这里转不出去。"""
        cycled = timeline.cycle_windows([{"start": 0.0, "end": 0.0}], 30.0)
        assert len(cycled) == 1
