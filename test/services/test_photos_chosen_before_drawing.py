"""开片时定"多少画面照着照片画"，开画之前一口气把照片挑完。

钉住的事：

* 表单多了一项，默认是 0（一张都不照着照片画），值跟着任务参数走，
  不记成"上次用的"；老任务读出来也是 0；
* 时间线把这个数告诉页面；
* 页面点"开画"时，有份额就先开挑照片的那一轮，一张图都不排；全挑完才排，
  照片那几段先排；有一张排不进去，其余的不排；
* "保留多少照片"那个下拉框在挑照片的窗里，不在右边那一栏。

A share of a new video's scenes can be drawn from photos the user chooses, and
they are all chosen before any picture is queued.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models.schema import NewVideoRequest, VideoParams
from app.services import new_video

from test.services.test_new_video import _fields, saved  # noqa: F401
from test.services.test_scene_editor import (  # noqa: F401
    _reviewed_task,
    task_root,
)

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "resource/public/editor"
JS = (PAGE / "editor.js").read_text(encoding="utf-8").replace("\r\n", "\n")
HTML = (PAGE / "index.html").read_text(encoding="utf-8").replace("\r\n", "\n")


def _body(name: str) -> str:
    for opener in (f"\nasync function {name}(", f"\nfunction {name}("):
        if opener in JS:
            return JS.split(opener)[1].split("\n}\n")[0]
    raise AssertionError(name)


class TestTheForm:
    def test_it_is_offered_for_drawn_pictures_and_starts_at_none(self, saved):  # noqa: F811
        form = new_video.form()
        field = _fields(form)["photo_scene_percent"]
        assert field["kind"] == "select"
        assert [one["id"] for one in field["options"]] == [0, 10, 25, 50, 75, 100]
        assert field["show"] == {"video_source": list(new_video.IMAGE_SOURCES)}
        assert form["values"]["photo_scene_percent"] == 0

    def test_it_is_not_behind_more_settings(self):
        assert "photo_scene_percent" not in new_video.ADVANCED_FIELDS

    def test_the_choice_goes_onto_the_video(self, saved):  # noqa: F811
        params = new_video.build_params(
            {"video_subject": "cats", "photo_scene_percent": 25})
        assert params.photo_scene_percent == 25

    def test_left_alone_it_is_none(self, saved):  # noqa: F811
        assert new_video.build_params({"video_subject": "cats"}).photo_scene_percent == 0

    def test_it_is_not_remembered_as_a_setting(self):
        assert "photo_scene_percent" not in new_video.UI_FIELDS
        assert "photo_scene_percent" not in new_video.APP_FIELDS

    def test_only_a_percentage_is_accepted(self):
        with pytest.raises(ValidationError):
            NewVideoRequest(photo_scene_percent=101)
        with pytest.raises(ValidationError):
            NewVideoRequest(photo_scene_percent=-1)

    def test_a_video_made_before_this_has_none(self):
        assert VideoParams(video_subject="x").photo_scene_percent == 0


class TestTheTimelineSaysIt:
    def test_the_page_is_told_the_share(self):
        source = (ROOT / "app/services/scene_editor.py").read_text(encoding="utf-8")
        assert 'payload["photo_scene_percent"] = int(' in source


class TestWhichScenes:
    """有真人、真地方、真东西的那几段，人排最前——不是隔几段取一段。"""

    def test_none_when_the_share_is_none(self):
        body = _body("photoQuota")
        assert "if (percent <= 0 || !all.length) return { count: 0, waiting: [] };" in body

    def test_the_share_is_of_the_whole_video_and_counts_those_already_done(self):
        body = _body("photoQuota")
        assert "Math.round(all.length * percent / 100) - done" in body
        assert "all.filter((scene) => scene.reference).length" in body
        assert "active.filter((job) => job.from_photo).length" in body
        assert "isPending(scene) && !busy.has(scene.index)" in body

    def test_the_model_is_asked_which_scenes_name_something_real(self):
        body = _body("scenesForPhotos")
        assert "/photos/candidates`" in body
        assert 'one.rating.kind !== "none"' in body
        assert "one.rating.score >= PHOTO_WORTH" in body
        assert "const PHOTO_WORTH = 6;" in JS

    def test_people_come_first_then_the_surest(self):
        body = _body("scenesForPhotos")
        people = body.index('(b.rating.kind === "person") - (a.rating.kind === "person")')
        assert people < body.index("b.rating.score - a.rating.score")

    def test_fewer_than_the_share_rather_than_scenes_that_name_nothing(self):
        body = _body("scenesForPhotos")
        assert "worth.slice(0, count)" in body
        # 不拿没有主体的那几段去凑数：没有"隔着取"了。
        assert "waiting.length / count" not in JS

    def test_they_are_gone_through_in_timeline_order(self):
        assert ".sort((a, b) => a.index - b.index);" in _body("scenesForPhotos")

    def test_a_model_that_does_not_answer_stops_the_draw(self):
        start = JS.index('ui.approveDraw.addEventListener("click"')
        click = JS[start:JS.index("function drawPending(", start)]
        failed = click.split("} catch (error) {")[1].split("} finally {")[0]
        assert "Nothing was queued" in failed and "return;" in failed
        assert "drawPending" not in failed and "startPhotoRound" not in failed

    def test_no_scene_worth_a_photo_is_said_and_everything_is_drawn(self):
        start = JS.index('ui.approveDraw.addEventListener("click"')
        click = JS[start:JS.index("function drawPending(", start)]
        assert 'drawPending("No scene left to draw names a real person' in click


class TestTheRating:
    """模型只管从每句台词里抄出一个名字；名字在不在那句话里是这边查的。"""

    SCENES = [("Santos Dumont flew in Paris", "a man by a biplane"),
              ("Inflation rose", "a rising line")]

    def _rate(self, answer, scenes=None):
        from unittest.mock import patch
        from app.services import llm

        with patch.object(llm, "_generate_response", return_value=answer) as asked:
            return llm.rate_scenes_for_photos("flight", scenes or self.SCENES), asked

    def test_one_answer_per_scene_in_order(self):
        rated, _ = self._rate("1|person|Santos Dumont\n2|none|-")
        assert rated == [{"kind": "person", "score": 9, "name": "Santos Dumont"},
                         {"kind": "none", "score": 0, "name": ""}]

    def test_the_model_reads_the_narration_not_the_invented_picture(self):
        _, asked = self._rate("1|person|Santos Dumont\n2|none|-")
        request = asked.call_args[0][0]
        assert "Santos Dumont flew in Paris" in request
        assert "a man by a biplane" not in request

    def test_it_is_asked_without_the_thinking_step(self):
        _, asked = self._rate("1|person|Santos Dumont\n2|none|-")
        assert asked.call_args.kwargs["without_thinking"] is True

    def test_text_around_the_answer_does_not_matter(self):
        rated, _ = self._rate("Here you go:\n 1 | Person | Santos Dumont \n2|none|-\nDone.")
        assert rated[0]["kind"] == "person"

    def test_a_name_that_is_not_in_the_line_does_not_count(self):
        scenes = [("He confessed to everything", ""), ("It was a lie", "")]
        rated, _ = self._rate("1|person|Dennis Rader\n2|person|Dennis Rader", scenes)
        assert [one["kind"] for one in rated] == ["none", "none"]

    def test_an_ordinary_noun_or_the_first_word_is_not_a_name(self):
        scenes = [("Inside was a purple floppy disk", ""), ("Then he vanished", ""),
                  ("In January two thousand four", "")]
        rated, _ = self._rate("1|thing|floppy disk\n2|person|Then\n3|event|January", scenes)
        assert [one["kind"] for one in rated] == ["none", "none", "none"]

    def test_a_line_that_is_only_a_name_counts(self):
        rated, _ = self._rate("1|person|Nancy Fox\n2|none|-", [("Nancy Fox", ""), ("x", "")])
        assert rated[0]["kind"] == "person"

    def test_a_name_seen_before_ranks_below_a_new_one(self):
        scenes = [("Police stopped Dennis Rader", ""), ("Then Dennis Rader confessed", "")]
        rated, _ = self._rate("1|person|Dennis Rader\n2|person|Dennis Rader", scenes)
        assert rated[0]["score"] > rated[1]["score"] >= 6

    def test_a_scene_the_model_skipped_is_an_error_not_a_no(self):
        from app.services import llm

        with pytest.raises(llm.TermGenerationError, match="answered for 1 of 2"):
            self._rate("1|person|Santos Dumont")

    def test_what_the_provider_says_went_wrong_is_passed_on(self):
        from app.services import llm

        with pytest.raises(llm.TermGenerationError, match="model not found"):
            self._rate("Error: model not found")

    def test_a_long_video_is_asked_in_parts(self):
        from unittest.mock import patch
        from app.services import llm

        scenes = [("a line", "prompt")] * (llm.PHOTO_RATING_CHUNK + 3)

        def answer(request, **_more):
            count = request.split("## Narration")[1].count(". a line")
            return "\n".join(f"{n}|none|-" for n in range(1, count + 1))

        with patch.object(llm, "_generate_response", side_effect=answer) as asked:
            rated = llm.rate_scenes_for_photos("x", scenes)
        assert asked.call_count == 2 and len(rated) == len(scenes)

    def test_only_the_local_model_is_told_to_skip_thinking(self):
        source = (ROOT / "app/services/llm.py").read_text(encoding="utf-8")
        assert 'if without_thinking and llm_provider == "ollama" else {}' in source

    def test_the_route_is_declared_before_the_one_that_would_swallow_it(self):
        source = (ROOT / "app/controllers/v1/editor.py").read_text(encoding="utf-8")
        assert source.index('"/tasks/{task_id}/photos/candidates"') < source.index(
            '"/tasks/{task_id}/photos/used/{name}"')


class TestDrawThePictures:
    def _click(self) -> str:
        start = JS.index('ui.approveDraw.addEventListener("click"')
        return JS[start:JS.index("function drawPending(", start)]

    def test_the_photos_are_chosen_before_anything_is_queued(self):
        click = self._click()
        assert "startPhotoRound(todo);" in click
        assert "if (!photoQuota().count) {\n    drawPending();" in click
        assert "/scenes/draw" not in click and "/material/queue" not in click

    def test_a_scene_whose_photo_is_already_chosen_is_not_asked_again(self):
        click = self._click()
        assert "found.scenes.filter((scene) => !scene.photo_pick)" in click
        assert "if (!todo.length) {\n    drawPending();" in click
        assert "scenes[at].photo_pick) at += 1;" in _body("nextRoundPhoto")

    def test_each_choice_is_kept_on_the_server_as_it_is_made(self):
        keep = _body("keepRoundPhoto")
        assert "`${scenePath(at)}/photo-pick`" in keep
        assert keep.index("/photo-pick") < keep.index("nextRoundPhoto();")
        assert "/material/queue" not in keep and "/scenes/draw" not in keep
        redraw = _body("redrawFromPhoto")
        assert redraw.index("if (photoRound.on) {") < redraw.index("queueRegeneration(")

    def test_a_photo_that_cannot_be_used_is_said_on_that_scene(self):
        keep = _body("keepRoundPhoto")
        failed = keep.split("} catch (error) {")[1].split("\n  }\n")[0]
        assert "Choose another photo for this scene." in failed
        assert "return;" in failed and "nextRoundPhoto" not in failed

    def test_the_queue_starts_only_after_the_last_one(self):
        following = _body("nextRoundPhoto")
        assert "if (at >= scenes.length) {\n    finishPhotoRound();" in following
        assert "drawPending();" in _body("finishPhotoRound")

    def test_one_request_draws_everything_and_the_server_knows_the_photos(self):
        draw = _body("drawPending")
        assert "/scenes/draw" in draw and "/material/queue" not in draw
        assert "result.from_photos" in draw

    def test_a_scene_can_be_skipped_and_is_then_drawn_from_its_prompt(self):
        assert "keepRoundPhoto({ skip: true })" in JS
        assert "pick.skip ? { skip: true }" in _body("keepRoundPhoto")
        assert 'id="photo-skip"' in HTML

    def test_closing_part_way_queues_nothing(self):
        close = _body("closePhotoPicker")
        assert "photoRound.on = false;" in close
        assert "Nothing was queued." in close


def _photo(data: bytes = b"png-bytes"):
    from app.services import reference_image

    return reference_image.Reference(
        data=data, query="santos dumont", source_url="https://site.test/a.png",
        page_url="https://site.test/page", provider="upload",
        width=800, height=600, licence="CC BY", creator="someone",
    )


class TestTheChosenPhotosAreKept:
    """挑好的照片存在这条片子里：以前只在内存里放八张，挑到第九张第一张就没了。"""

    def test_the_photo_and_the_choice_are_written_down(self, task_root):  # noqa: F811
        from app.services import scene_editor, scene_manifest

        task_id, _ = _reviewed_task(task_root)
        got = scene_editor.keep_photo_pick(
            task_id, 0, photo=_photo(), search="santos dumont", follow="picture")
        assert got["pick"] == "photo"
        key = scene_manifest.scene_id(scene_manifest.load(task_id)["scenes"][0])
        ready = scene_editor.photo_picks_for_drawing(task_id)[key]
        assert ready["from_photo"] is True and ready["follow"] == "picture"
        assert ready["search"] == "santos dumont"
        assert ready["photo"].data == b"png-bytes"
        assert ready["photo"].page_url == "https://site.test/page"
        assert ready["photo"].licence == "CC BY"
        folder = task_root / task_id / scene_editor.PHOTO_PICK_DIR
        assert len(list(folder.iterdir())) == 1

    def test_many_can_be_chosen_without_losing_the_first(self, task_root):  # noqa: F811
        from app.services import scene_editor

        task_id, _ = _reviewed_task(task_root, scenes=12)
        for at in range(12):
            scene_editor.keep_photo_pick(
                task_id, at, photo=_photo(f"photo-{at}".encode()), strength=0.55)
        ready = scene_editor.photo_picks_for_drawing(task_id)
        assert sorted(one["photo"].data for one in ready.values()) == sorted(
            f"photo-{at}".encode() for at in range(12))

    def test_it_does_not_need_the_app_to_stay_up(self, task_root):  # noqa: F811
        from app.services import scene_editor

        task_id, _ = _reviewed_task(task_root)
        scene_editor.keep_photo_pick(task_id, 1, photo=_photo(), strength=0.95)
        # 内存里那份清空了（程序重启），存着的还在。
        scene_editor._UPLOADED.clear()
        assert len(scene_editor.photo_picks_for_drawing(task_id)) == 1

    def test_skipping_and_picking_for_me(self, task_root):  # noqa: F811
        from app.services import scene_editor

        task_id, _ = _reviewed_task(task_root)
        assert scene_editor.keep_photo_pick(task_id, 0, skip=True)["pick"] == "skip"
        assert scene_editor.keep_photo_pick(task_id, 1, search="eiffel tower")["pick"] == "auto"
        ready = list(scene_editor.photo_picks_for_drawing(task_id).values())
        assert len(ready) == 1 and ready[0]["photo"] is None
        assert ready[0]["search"] == "eiffel tower"

    def test_the_timeline_says_which_scenes_are_settled(self, task_root):  # noqa: F811
        from app.services import scene_editor

        task_id, _ = _reviewed_task(task_root)
        scene_editor.keep_photo_pick(task_id, 1, photo=_photo())
        scenes = scene_editor.describe(task_id)["scenes"]
        assert [one["photo_pick"] for one in scenes] == ["", "photo"]

    def test_a_chosen_photo_that_cannot_be_read_back_is_an_error(self, task_root):  # noqa: F811
        from app.services import scene_editor, scene_manifest

        task_id, _ = _reviewed_task(task_root)
        scene_editor.keep_photo_pick(task_id, 0, photo=_photo())
        for kept in (task_root / task_id / scene_editor.PHOTO_PICK_DIR).iterdir():
            kept.unlink()
        with pytest.raises(scene_manifest.SceneError, match="could not be read back"):
            scene_editor.photo_picks_for_drawing(task_id)

    def test_drawing_uses_them_and_puts_them_first(self):
        source = (ROOT / "app/services/scene_regen_queue.py").read_text(encoding="utf-8")
        pending = source.split("def enqueue_pending(")[1].split("\ndef ")[0]
        assert "scene_editor.photo_picks_for_drawing(task_id)" in pending
        assert '**picks.get(plan["scene_id"], {}),' in pending
        assert "0 if job.follow else 1 if job.from_photo else 2" in pending
        assert '"from_photos": sum(1 for job in queued if job.from_photo),' in pending

    def test_over_http(self, task_root):  # noqa: F811
        from fastapi.testclient import TestClient
        from app import asgi
        from app.services import scene_editor

        task_id, _ = _reviewed_task(task_root)
        got = TestClient(asgi.app).post(
            f"/api/v1/tasks/{task_id}/scenes/0/photo-pick", json={"skip": True})
        assert got.status_code == 200, got.text
        assert scene_editor.photo_pick_kinds(task_id) != {}
        bad = TestClient(asgi.app).post(
            f"/api/v1/tasks/{task_id}/scenes/0/photo-pick",
            json={"photo_upload": "not-a-real-token"})
        assert bad.status_code == 400 and "no longer here" in bad.text


class TestTheScenesAreFoundOnce:
    """哪几段点了真实的名字：场景切出来时算一次、记下来，之后只是读。"""

    RATED = [{"kind": "person", "score": 9, "name": "Santos Dumont"},
             {"kind": "none", "score": 0, "name": ""}]

    def test_asked_once_then_read(self, task_root):  # noqa: F811
        from unittest.mock import patch
        from app.services import llm, scene_editor

        task_id, _ = _reviewed_task(task_root)
        with patch.object(llm, "rate_scenes_for_photos", return_value=self.RATED) as asked:
            first = scene_editor.photo_candidates(task_id)
            again = scene_editor.photo_candidates(task_id)
        assert asked.call_count == 1 and first == again
        assert [one["kind"] for one in first["scenes"]] == ["person", "none"]

    def test_a_failure_is_not_remembered_as_an_answer(self, task_root):  # noqa: F811
        from unittest.mock import patch
        from app.services import llm, scene_editor, scene_manifest

        task_id, _ = _reviewed_task(task_root)
        with patch.object(llm, "rate_scenes_for_photos",
                          side_effect=llm.TermGenerationError("down")):
            with pytest.raises(scene_manifest.SceneError, match="down"):
                scene_editor.photo_candidates(task_id)
        with patch.object(llm, "rate_scenes_for_photos", return_value=self.RATED) as asked:
            scene_editor.photo_candidates(task_id)
        assert asked.call_count == 1

    def test_the_pipeline_does_it_when_the_scenes_are_split(self):
        source = (ROOT / "app/services/task.py").read_text(encoding="utf-8")
        planned = source.index("scene_manifest.record_planned_scenes(")
        assert 0 < source.index("_find_scenes_for_photos(task_id, params)", planned) - planned < 600

    def test_only_for_a_video_that_asked_for_photos(self, task_root):  # noqa: F811
        from types import SimpleNamespace
        from unittest.mock import patch
        from app.services import scene_editor, task

        with patch.object(scene_editor, "photo_candidates") as asked:
            task._find_scenes_for_photos("t", SimpleNamespace(photo_scene_percent=0))
            assert not asked.called
            task._find_scenes_for_photos("t", SimpleNamespace(photo_scene_percent=25))
            assert asked.call_args.kwargs == {"again": True}

    def test_a_model_that_does_not_answer_does_not_stop_the_video(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from app.services import scene_editor, task

        with patch.object(scene_editor, "photo_candidates", side_effect=RuntimeError("down")):
            task._find_scenes_for_photos("t", SimpleNamespace(photo_scene_percent=25))


class TestTheChoiceIsInThePhotoWindow:
    def test_the_window_has_the_place_for_it(self):
        assert 'id="photo-keep"' in HTML
        assert "ui.photoKeepSlot.appendChild(photoKeepChoice.wrap);" in JS

    def test_it_is_the_only_one(self):
        assert JS.count("photoKeepPicker()") == 2  # 定义一次，用一次

    def test_the_inspector_no_longer_has_it(self):
        start = JS.index('inspectorGroup("Draw it from a photo")')
        block = JS[start:JS.index('button("Earlier media…"', start)]
        assert "photoKeepPicker" not in block and "keep.wrap" not in block

    def test_it_opens_on_the_last_choice(self):
        assert "photoKeepChoice.input.value = String(photoKeep);" in _body(
            "openPhotoPicker")

    def test_the_window_says_which_line_the_photo_is_for(self):
        assert 'id="photo-line"' in HTML
        assert "ui.photoLine.textContent = photoSceneLine(scene);" in _body(
            "openPhotoPicker")


class TestRedrawingFromTheSavedPhoto:
    """"照这同一张再画"也认开画之前存下的那一张，不只认照着画过的那一张。"""

    def test_the_button_is_there_for_a_saved_photo_too(self):
        assert 'const saved = !kept && scene.photo_pick === "photo";' in JS
        assert "if (kept || saved) {" in JS
        assert "if (saved) photoBox.appendChild(savedPhotoNote(scene));" in JS

    def test_the_window_offers_it_and_sends_it(self):
        opened = _body("openPhotoPicker")
        assert 'saved: scene.photo_pick === "photo",' in opened
        assert "ui.photoKept.hidden = round || !(photoPick.kept || photoPick.saved);" in opened
        assert "else if (photoPick.saved) redrawFromPhoto({ photo_picked: true });" in JS

    def test_the_photo_drawn_from_before_still_comes_first(self):
        click = JS.split('ui.photoKept.addEventListener("click"')[1][:400]
        assert click.index("photo_kept: photoPick.kept") < click.index("photo_picked: true")

    def test_the_saved_photo_is_what_gets_drawn_from(self, task_root):  # noqa: F811
        from unittest.mock import patch
        from fastapi.testclient import TestClient
        from app import asgi
        from app.services import scene_editor, scene_regen_queue

        task_id, _ = _reviewed_task(task_root)
        scene_editor.keep_photo_pick(task_id, 1, photo=_photo(b"the saved one"))
        seen = {}

        def enqueue(*_args, **kwargs):
            seen.update(kwargs)
            return {"id": "job"}

        with patch.object(scene_regen_queue, "enqueue", side_effect=enqueue):
            got = TestClient(asgi.app).post(
                f"/api/v1/tasks/{task_id}/scenes/1/material/queue",
                json={"from_photo": True, "photo_picked": True, "follow": "picture"},
            )
        assert got.status_code == 200, got.text
        assert seen["photo"].data == b"the saved one" and seen["follow"] == "picture"

    def test_a_scene_with_nothing_saved_says_so(self, task_root):  # noqa: F811
        from fastapi.testclient import TestClient
        from app import asgi

        task_id, _ = _reviewed_task(task_root)
        got = TestClient(asgi.app).post(
            f"/api/v1/tasks/{task_id}/scenes/0/material/queue",
            json={"from_photo": True, "photo_picked": True},
        )
        assert got.status_code == 400 and "no photo is saved for this scene" in got.text

    def test_the_saved_photo_can_be_looked_at(self, task_root):  # noqa: F811
        from fastapi.testclient import TestClient
        from app import asgi
        from app.services import scene_editor

        task_id, _ = _reviewed_task(task_root)
        scene_editor.keep_photo_pick(task_id, 0, photo=_photo(b"png"))
        client = TestClient(asgi.app)
        assert client.get(f"/api/v1/tasks/{task_id}/scenes/0/photo-pick").content == b"png"
        assert client.get(f"/api/v1/tasks/{task_id}/scenes/1/photo-pick").status_code == 400

