"""发到 YouTube 之前要准备的标题、简介和封面。

Why these tests exist
---------------------
封面的形状由这条片子的画幅决定，而不是由配置决定。这件事很容易在实现里丢掉：
出图的那个端点本来就有一项 ``openai_image_size`` 配置，顺手复用它最省事——
而那一项是为场景素材配的，用户为竖版素材把它设成 1024x1536 之后，一条 16:9
的片子会得到一张竖封面，传到 YouTube 两边一裁，主体正好被裁没。

所以这组测试钉住三件事：

* 画幅从任务清单里存着的那份参数读，不从"现在"的全局配置读；
* 请求给模型的尺寸和落盘的成品尺寸都跟着画幅走，不跟着 ``openai_image_size``；
* 这些东西一个字节都不进成片——写过文案的任务，渲染读到的剪辑表仍然是空的。
"""

import io
import os
from unittest.mock import patch

import pytest
from PIL import Image

from app.models.schema import VideoAspect, VideoParams
from app.services import (
    llm,
    material,
    publish_metadata,
    scene_editor,
    scene_manifest,
    task_artifacts,
    thumbnail_text,
)
from app.utils import utils

from test.services.test_scene_editor import (  # noqa: F401
    _reviewed_task,
    idle_task,
    task_root,
)


def _task(task_root, aspect="9:16", subject="seeds", script="One. Two."):
    """造一个已经跑到素材阶段、画幅明确的任务。"""
    task_id, _ = _reviewed_task(task_root)
    params = VideoParams(
        video_subject=subject,
        video_script=script,
        video_source="openai_image",
        video_aspect=aspect,
        video_clip_duration=5,
    )
    task_artifacts.patch_script_data(task_id, params=params.model_dump())
    return task_id


def _png(width=512, height=512, colour=(10, 120, 200)):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="PNG")
    return buffer.getvalue()


class TestTheAspectRatioIsKnownFirst:
    def test_the_aspect_comes_from_the_task_and_not_the_configuration(self, task_root):
        """编辑器跑在 API 进程里，那份配置可能早就不是出片时的那一份了。"""
        task_id = _task(task_root, aspect="16:9")
        with patch.dict(material.config.app, {"video_aspect": "9:16"}, clear=False):
            assert publish_metadata.aspect_of(task_id) == "16:9"

    def test_an_unreadable_aspect_falls_back_to_portrait(self, task_root):
        task_id, _ = _reviewed_task(task_root)
        task_artifacts.patch_script_data(task_id, params={"video_aspect": "3:2"})
        assert publish_metadata.aspect_of(task_id) == "9:16"

    def test_a_task_without_parameters_falls_back_to_portrait(self, task_root):
        task_artifacts.write_script_data("bare", {"script": "s"})
        assert publish_metadata.aspect_of("bare") == "9:16"

    @pytest.mark.parametrize(
        "aspect,kind,size",
        [
            ("9:16", "short", (1080, 1920)),
            ("16:9", "standard", (1280, 720)),
            ("1:1", "square", (1080, 1080)),
        ],
    )
    def test_the_panel_is_told_the_shape_before_anything_is_drawn(
        self, task_root, aspect, kind, size
    ):
        """用户要在按下"画封面"之前就知道会得到一张什么形状的图。"""
        task_id = _task(task_root, aspect=aspect)
        data = publish_metadata.describe(task_id)
        assert data["aspect"] == aspect
        assert data["kind"] == kind
        assert (data["thumbnail_width"], data["thumbnail_height"]) == size

    def test_a_vertical_video_is_packaged_as_a_short(self):
        assert llm.youtube_format("9:16")["kind"] == "short"
        assert llm.youtube_format("16:9")["kind"] == "standard"

    def test_the_prompt_tells_the_model_the_aspect_and_its_framing(self):
        portrait = llm.build_youtube_metadata_prompt("seeds", "x", aspect="9:16")
        landscape = llm.build_youtube_metadata_prompt("seeds", "x", aspect="16:9")
        assert "9:16" in portrait and "TALL" in portrait
        assert "16:9" in landscape and "WIDE" in landscape


class TestWritingTheText:
    def test_the_aspect_is_handed_to_the_model(self, task_root):
        task_id = _task(task_root, aspect="16:9")
        captured = {}

        def fake(video_subject, video_script="", aspect="", language="auto"):
            captured.update(
                subject=video_subject, script=video_script, aspect=aspect
            )
            return {
                "title": "A title",
                "description": "A description",
                "tags": ["one", "two"],
                "thumbnail_prompt": "A picture of seeds",
            }

        with patch.object(llm, "generate_youtube_metadata", side_effect=fake):
            data = publish_metadata.generate_text(task_id)

        assert captured["aspect"] == "16:9"
        assert captured["subject"] == "seeds"
        assert captured["script"] == "One. Two."
        assert data["title"] == "A title"
        assert data["tags"] == ["one", "two"]

    def test_the_text_survives_in_the_manifest(self, task_root):
        task_id = _task(task_root)
        with patch.object(
            llm,
            "generate_youtube_metadata",
            return_value={
                "title": "T",
                "description": "D",
                "tags": ["a"],
                "thumbnail_prompt": "P",
            },
        ):
            publish_metadata.generate_text(task_id)
        stored = scene_manifest.load_raw(task_id)["publish"]
        assert stored["title"] == "T"
        assert stored["thumbnail_prompt"] == "P"
        assert publish_metadata.describe(task_id)["description"] == "D"

    def test_a_task_with_nothing_to_say_is_a_named_error(self, task_root):
        task_id = _task(task_root, subject="", script="")
        task_artifacts.patch_script_data(task_id, script="")
        with pytest.raises(publish_metadata.SceneError) as error:
            publish_metadata.generate_text(task_id)
        assert "nothing" in str(error.value)

    def test_a_broken_model_still_yields_an_editable_draft(self):
        """空面板分不清是模型坏了还是功能坏了，所以降级也要给出草稿。"""
        with patch.object(llm, "_generate_response", return_value="not json"):
            data = llm.generate_youtube_metadata("seeds", "A sentence.", aspect="9:16")
        assert data["title"] == "seeds"
        assert "#Shorts" in data["description"]
        assert data["thumbnail_prompt"]

    def test_tags_are_search_phrases_and_not_hashtags(self):
        """YouTube 的标签框接受带空格的词组，挤掉空格只会让它们更难被搜到。"""
        tags = llm._normalize_youtube_tags(["#street food", "street food", "x"], 10)
        assert tags == ["street food", "x"]


class TestEditingTheText:
    def test_an_over_long_title_is_refused_and_nothing_is_stored(self, task_root):
        task_id = _task(task_root)
        with pytest.raises(publish_metadata.SceneError) as error:
            publish_metadata.set_metadata(task_id, title="x" * 101)
        assert "101 characters" in str(error.value)
        assert "publish" not in scene_manifest.load_raw(task_id)

    def test_only_the_submitted_fields_change(self, task_root):
        task_id = _task(task_root)
        publish_metadata.set_metadata(task_id, title="T", description="D")
        publish_metadata.set_metadata(task_id, title="Another")
        data = publish_metadata.describe(task_id)
        assert data["title"] == "Another"
        assert data["description"] == "D"

    def test_tags_arrive_as_a_comma_separated_line_from_the_panel(self, task_root):
        task_id = _task(task_root)
        data = publish_metadata.set_metadata(task_id, tags=" #one , two words ,one ")
        assert data["tags"] == ["one", "two words"]

    def test_tags_stop_at_youtube_s_total_length(self, task_root):
        task_id = _task(task_root)
        data = publish_metadata.set_metadata(
            task_id, tags=[f"{'t' * 90}{index}" for index in range(10)]
        )
        assert sum(len(tag) for tag in data["tags"]) <= publish_metadata.MAX_TAGS_LENGTH
        assert all(tag for tag in data["tags"])


class TestDrawingTheThumbnail:
    def test_the_requested_size_follows_the_aspect_and_not_the_configuration(
        self, task_root
    ):
        """``openai_image_size`` 是为场景素材配的，封面不能跟着它走。"""
        task_id = _task(task_root, aspect="16:9")
        publish_metadata.set_metadata(task_id, thumbnail_prompt="A field of seeds")
        seen = {}

        def fake(endpoint, payload):
            seen.update(payload)
            return _png(1536, 1024), ""

        with patch.dict(
            material.config.app,
            {
                "openai_image_base_url": "http://local",
                "openai_image_model": "model",
                "openai_image_size": "1024x1536",
            },
            clear=False,
        ):
            with patch.object(material, "_request_openai_image", side_effect=fake):
                data = publish_metadata.generate_thumbnail(task_id)

        assert seen["size"] == "1536x1024"
        assert seen["prompt"] == "A field of seeds"
        assert (data["thumbnail_width"], data["thumbnail_height"]) == (1280, 720)

    def test_the_saved_file_is_exactly_the_shape_youtube_expects(self, task_root):
        task_id = _task(task_root, aspect="9:16")
        with patch.dict(
            material.config.app,
            {"openai_image_base_url": "http://local", "openai_image_model": "model"},
            clear=False,
        ):
            with patch.object(
                material, "_request_openai_image", return_value=(_png(600, 400), "")
            ):
                data = publish_metadata.generate_thumbnail(
                    task_id, prompt="A sprouting seed"
                )

        path = publish_metadata.thumbnail_file(task_id)
        assert os.path.isfile(path)
        with Image.open(path) as image:
            assert image.size == (1080, 1920)
        # 固定文件名，所以地址要带一个会变的参数，否则浏览器接着显示上一张。
        assert data["thumbnail_url"].startswith("/api/v1/stream/")
        assert "?v=" in data["thumbnail_url"]

    def test_regenerating_replaces_the_one_thumbnail(self, task_root):
        task_id = _task(task_root)
        with patch.dict(
            material.config.app,
            {"openai_image_base_url": "http://local", "openai_image_model": "model"},
            clear=False,
        ):
            with patch.object(
                material, "_request_openai_image", return_value=(_png(), "")
            ):
                publish_metadata.generate_thumbnail(task_id, prompt="one")
                publish_metadata.generate_thumbnail(task_id, prompt="two")
        directory = os.path.dirname(publish_metadata.thumbnail_file(task_id))
        drawn = sorted(name for name in os.listdir(directory) if "thumbnail" in name)
        # 固定的两个名字：压字前那张图，和压好字的成品。重出一张是替换它们，
        # 不是在目录里攒一堆候选。
        assert drawn == [
            publish_metadata.BASE_FILENAME,
            publish_metadata.THUMBNAIL_FILENAME,
        ]
        assert publish_metadata.describe(task_id)["thumbnail_prompt"] == "two"

    def test_drawing_without_a_description_is_a_named_error(self, task_root):
        """主题原样去出图得到的是一张"插画化的句子"，不是一张封面。"""
        task_id = _task(task_root)
        with pytest.raises(publish_metadata.SceneError) as error:
            publish_metadata.generate_thumbnail(task_id)
        assert "no thumbnail description" in str(error.value)

    def test_a_failing_image_service_is_named_and_leaves_no_thumbnail(self, task_root):
        task_id = _task(task_root)
        with patch.dict(
            material.config.app,
            {"openai_image_base_url": "http://local", "openai_image_model": "model"},
            clear=False,
        ):
            with patch.object(
                material, "_request_openai_image", return_value=(None, "503 from gateway")
            ):
                with pytest.raises(publish_metadata.SceneError) as error:
                    publish_metadata.generate_thumbnail(task_id, prompt="a seed")
        assert "503 from gateway" in str(error.value)
        assert publish_metadata.describe(task_id)["thumbnail"] == ""

    def test_a_response_that_is_not_an_image_is_named(self, task_root):
        task_id = _task(task_root)
        with patch.dict(
            material.config.app,
            {"openai_image_base_url": "http://local", "openai_image_model": "model"},
            clear=False,
        ):
            with patch.object(
                material, "_request_openai_image", return_value=(b"<html>no</html>", "")
            ):
                with pytest.raises(publish_metadata.SceneError) as error:
                    publish_metadata.generate_thumbnail(task_id, prompt="a seed")
        assert "not return a usable image" in str(error.value)

    def test_an_image_service_that_is_not_running_is_a_named_400(self, task_root):
        """这是实装后第一个真实失败：出图服务没开着。

        ``_request_openai_image`` 的失败有两条路——重试到头是**返回**
        ``(None, detail)``，而"请求可能已经送达但没拿回结果"是**抛**
        ``OpenAIImagePaidResultError``。只接住前一条的话，连接被拒会穿过
        FastAPI 变成一个没有响应体的 500，编辑器拿不到任何可显示的文案，
        按钮按下去看起来像什么都没发生。
        """
        import requests

        task_id = _task(task_root)
        refused = material.OpenAIImageUnconfirmedError("unconfirmed image request")
        refused.__cause__ = requests.exceptions.ConnectionError("refused")

        with patch.dict(
            material.config.app,
            {
                "openai_image_base_url": "http://127.0.0.1:8730/v1",
                "openai_image_model": "zimage-turbo",
            },
            clear=False,
        ):
            with patch.object(material, "_request_openai_image", side_effect=refused):
                with pytest.raises(publish_metadata.SceneError) as error:
                    publish_metadata.generate_thumbnail(task_id, prompt="a seed")

        message = str(error.value)
        # 要说的是哪个地址没人应答，而不是一段关于重复计费的说明。
        assert "127.0.0.1:8730" in message
        assert "not answering" in message
        assert "double billing" not in message
        assert publish_metadata.describe(task_id)["thumbnail"] == ""

    def test_any_other_unconfirmed_failure_is_still_named_and_not_a_crash(
        self, task_root
    ):
        """重定向等"结果未确认"的情形也不能变成 500。"""
        task_id = _task(task_root)
        with patch.dict(
            material.config.app,
            {"openai_image_base_url": "http://local", "openai_image_model": "model"},
            clear=False,
        ):
            with patch.object(
                material,
                "_request_openai_image",
                side_effect=material.OpenAIImageUnconfirmedError(
                    "image submission returned a redirect"
                ),
            ):
                with pytest.raises(publish_metadata.SceneError) as error:
                    publish_metadata.generate_thumbnail(task_id, prompt="a seed")
        assert "redirect" in str(error.value)

    def test_a_missing_endpoint_says_what_to_configure(self, task_root):
        task_id = _task(task_root)
        with patch.dict(
            material.config.app,
            {"openai_image_base_url": "", "openai_image_model": ""},
            clear=False,
        ):
            with pytest.raises(publish_metadata.SceneError) as error:
                publish_metadata.generate_thumbnail(task_id, prompt="a seed")
        assert "openai_image_base_url" in str(error.value)


class TestItDoesNotTouchTheVideo:
    def test_the_render_is_unaffected_by_anything_in_this_panel(self, task_root):
        """文案和封面不进成片：没编辑过画面的任务仍然不写剪辑表。"""
        task_id = _task(task_root)
        publish_metadata.set_metadata(
            task_id, title="T", description="D", tags=["a"], thumbnail_prompt="P"
        )
        assert scene_manifest.edit_list_for_render(task_id) == []

    def test_the_render_parameters_are_left_alone(self, task_root):
        task_id = _task(task_root, aspect="16:9")
        before = scene_manifest.load_raw(task_id)["params"]
        publish_metadata.set_metadata(task_id, title="T")
        after = scene_manifest.load_raw(task_id)["params"]
        assert after == before
        assert scene_manifest.load_raw(task_id)["params"]["video_aspect"] == "16:9"

    def test_the_scenes_are_left_alone(self, task_root):
        task_id = _task(task_root)
        before = scene_manifest.load(task_id)["scenes"]
        publish_metadata.set_metadata(task_id, description="D")
        assert scene_manifest.load(task_id)["scenes"] == before

    def test_the_thumbnail_is_not_a_scene(self, task_root):
        task_id = _task(task_root)
        with patch.dict(
            material.config.app,
            {"openai_image_base_url": "http://local", "openai_image_model": "model"},
            clear=False,
        ):
            with patch.object(
                material, "_request_openai_image", return_value=(_png(), "")
            ):
                publish_metadata.generate_thumbnail(task_id, prompt="a seed")
        materials = scene_manifest.materials_for_render(task_id)
        assert all(
            publish_metadata.THUMBNAIL_FILENAME not in path for path in materials
        )

    def test_writing_the_text_while_the_task_renders_is_allowed(self, task_root):
        """等渲染的那几分钟正是写文案最自然的时候，不该把它挡掉。"""
        task_id = _task(task_root)
        with patch.object(
            publish_metadata.scene_editor.sm.state, "get_task", return_value={"state": 1}
        ):
            with patch.object(
                publish_metadata.scene_editor.tm, "is_task_busy", return_value=True
            ):
                data = publish_metadata.set_metadata(task_id, title="Written while busy")
        assert data["title"] == "Written while busy"


class TestTheAspectIsAlsoStoredWithTheText:
    def test_the_panel_reports_the_image_service_availability(self, task_root):
        task_id = _task(task_root)
        with patch.dict(
            material.config.app,
            {"openai_image_base_url": "", "openai_image_model": ""},
            clear=False,
        ):
            assert publish_metadata.describe(task_id)["can_draw_thumbnail"] is False
        with patch.dict(
            material.config.app,
            {"openai_image_base_url": "http://local", "openai_image_model": "m"},
            clear=False,
        ):
            assert publish_metadata.describe(task_id)["can_draw_thumbnail"] is True

    def test_a_task_with_no_publishing_details_reads_back_empty(self, task_root):
        task_id = _task(task_root)
        data = publish_metadata.describe(task_id)
        assert data["title"] == ""
        assert data["tags"] == []
        assert data["thumbnail_url"] == ""
        assert data["has_subject"] is True

    def test_asking_for_a_thumbnail_file_that_was_never_drawn(self, task_root):
        task_id = _task(task_root)
        with pytest.raises(publish_metadata.SceneError) as error:
            publish_metadata.thumbnail_file(task_id)
        assert "no thumbnail yet" in str(error.value)

    def test_a_thumbnail_deleted_from_disk_is_reported_as_gone(self, task_root):
        task_id = _task(task_root)
        with patch.dict(
            material.config.app,
            {"openai_image_base_url": "http://local", "openai_image_model": "model"},
            clear=False,
        ):
            with patch.object(
                material, "_request_openai_image", return_value=(_png(), "")
            ):
                publish_metadata.generate_thumbnail(task_id, prompt="a seed")
        os.remove(publish_metadata.thumbnail_file(task_id))
        # 文件没了，面板不该给出一个必然 404 的地址。
        assert publish_metadata.describe(task_id)["thumbnail_url"] == ""



class TestOverHttp:
    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from app import asgi

        return TestClient(asgi.app)

    def test_the_panel_reads_the_aspect_over_http(self, client, task_root):
        task_id = _task(task_root, aspect="16:9")
        response = client.get(f"/api/v1/tasks/{task_id}/publish")
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["aspect"] == "16:9"
        assert data["thumbnail_width"] == 1280

    def test_saving_the_text_over_http(self, client, task_root):
        task_id = _task(task_root)
        response = client.put(
            f"/api/v1/tasks/{task_id}/publish",
            json={"title": "A title", "tags": "one, two"},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["title"] == "A title"
        assert data["tags"] == ["one", "two"]

    def test_an_over_long_title_is_a_400(self, client, task_root):
        task_id = _task(task_root)
        response = client.put(
            f"/api/v1/tasks/{task_id}/publish", json={"title": "x" * 101}
        )
        assert response.status_code == 400
        assert "YouTube allows 100" in response.text

    def test_writing_the_text_over_http(self, client, task_root):
        task_id = _task(task_root)
        with patch.object(
            llm,
            "generate_youtube_metadata",
            return_value={
                "title": "T",
                "description": "D",
                "tags": [],
                "thumbnail_prompt": "P",
            },
        ):
            response = client.post(f"/api/v1/tasks/{task_id}/publish/text", json={})
        assert response.status_code == 200
        assert response.json()["data"]["title"] == "T"

    def test_drawing_the_thumbnail_over_http(self, client, task_root):
        task_id = _task(task_root, aspect="16:9")
        with patch.dict(
            material.config.app,
            {"openai_image_base_url": "http://local", "openai_image_model": "model"},
            clear=False,
        ):
            with patch.object(
                material, "_request_openai_image", return_value=(_png(), "")
            ):
                response = client.post(
                    f"/api/v1/tasks/{task_id}/publish/thumbnail",
                    json={"prompt": "A field of seeds"},
                )
        assert response.status_code == 200
        assert response.json()["data"]["thumbnail_height"] == 720

    def test_the_thumbnail_downloads_with_a_name_naming_the_task(
        self, client, task_root
    ):
        """上传封面时要在文件选择框里认出它是哪条片子的。"""
        task_id = _task(task_root)
        with patch.dict(
            material.config.app,
            {"openai_image_base_url": "http://local", "openai_image_model": "model"},
            clear=False,
        ):
            with patch.object(
                material, "_request_openai_image", return_value=(_png(), "")
            ):
                client.post(
                    f"/api/v1/tasks/{task_id}/publish/thumbnail",
                    json={"prompt": "A seed"},
                )
        response = client.get(f"/api/v1/tasks/{task_id}/publish/thumbnail")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert f"thumbnail-{task_id}.png" in response.headers["content-disposition"]

    def test_downloading_a_thumbnail_that_does_not_exist_is_a_400(
        self, client, task_root
    ):
        task_id = _task(task_root)
        response = client.get(f"/api/v1/tasks/{task_id}/publish/thumbnail")
        assert response.status_code == 400
        assert "no thumbnail yet" in response.text

    def test_an_unreachable_image_service_reaches_the_panel_as_text(
        self, client, task_root
    ):
        """界面要拿到一句能显示的话。一个空响应体的 500 不是一句话。"""
        import requests

        task_id = _task(task_root)
        refused = material.OpenAIImageUnconfirmedError("unconfirmed image request")
        refused.__cause__ = requests.exceptions.ConnectionError("refused")

        with patch.dict(
            material.config.app,
            {
                "openai_image_base_url": "http://127.0.0.1:8730/v1",
                "openai_image_model": "zimage-turbo",
            },
            clear=False,
        ):
            with patch.object(material, "_request_openai_image", side_effect=refused):
                response = client.post(
                    f"/api/v1/tasks/{task_id}/publish/thumbnail",
                    json={"prompt": "A field of seeds"},
                )
        assert response.status_code == 400
        assert "not answering" in response.json()["message"]

    def test_an_unknown_task_is_a_404(self, client, task_root):
        response = client.get("/api/v1/tasks/nope/publish")
        assert response.status_code == 404

def test_every_aspect_has_a_thumbnail_size():
    """画幅和封面尺寸这两张表必须对得上，否则换画幅就是一个 KeyError。"""
    for aspect in VideoAspect:
        assert aspect in material.YOUTUBE_THUMBNAIL_PIXELS
        assert aspect in material.YOUTUBE_THUMBNAIL_REQUEST_SIZES
        assert aspect.value in llm.YOUTUBE_FORMATS


class TestSayingWhenNoModelWroteIt:
    """降级拼出来的草稿必须署名，否则"模型没跑"看起来就像"模型很差"。"""

    def test_a_model_written_draft_is_marked_as_one(self, task_root):
        task_id = _task(task_root)
        with patch.object(
            llm,
            "_generate_response",
            return_value='{"title":"T","description":"D","tags":["a"],'
            '"thumbnail_prompt":"P"}',
        ):
            data = publish_metadata.generate_text(task_id)
        assert data["text_source"] == "model"
        assert data["text_error"] == ""

    def test_a_fallback_draft_says_so_and_why(self, task_root):
        """这正是实装当天发生的事：ollama 没配 model_name。"""
        task_id = _task(task_root)
        with patch.object(
            llm,
            "_generate_response",
            return_value="Error: ollama: model_name is not set, please set it in "
            "the config.toml file.",
        ):
            data = publish_metadata.generate_text(task_id)
        assert data["text_source"] == "fallback"
        assert "model_name is not set" in data["text_error"]

    def test_the_reason_survives_so_tomorrow_s_panel_still_explains_it(
        self, task_root
    ):
        """草稿在磁盘上一直是这一份，重新打开面板时也该说清楚为什么它很差。"""
        task_id = _task(task_root)
        with patch.object(
            llm, "_generate_response", return_value="Error: nothing is configured"
        ):
            publish_metadata.generate_text(task_id)
        reopened = publish_metadata.describe(task_id)
        assert reopened["text_source"] == "fallback"
        assert "nothing is configured" in reopened["text_error"]

    def test_a_malformed_reply_also_reports_a_reason(self, task_root):
        task_id = _task(task_root)
        with patch.object(llm, "_generate_response", return_value="not json at all"):
            data = publish_metadata.generate_text(task_id)
        assert data["text_source"] == "fallback"
        assert data["text_error"]


def _task_with_real_pictures(task_root, aspect="9:16", scenes=3):
    """一个场景素材是真图片的任务。

    ``_reviewed_task`` 写的是占位字节（``b"x"``），拿它当图片读会失败——而这
    组用例要测的正是"把那张图裁成封面"，所以这里写真的 PNG。
    """
    task_id = _task(task_root, aspect=aspect)
    directory = task_root / task_id
    materials = []
    for index in range(scenes):
        path = directory / f"picture-{index}.png"
        # 每一张给不同的颜色，用来确认裁出来的确实是点中的那一张。
        Image.new("RGB", (900, 1600), (index * 70, 40, 200 - index * 40)).save(path)
        materials.append(str(path))
    manifest = scene_manifest.load_raw(task_id)
    manifest["scenes"] = [
        {
            "index": index,
            "material": path,
            "kind": "image",
            "keyword": f"keyword {index}",
            "narration": "",
        }
        for index, path in enumerate(materials)
    ]
    task_artifacts.write_script_data(task_id, manifest)
    return task_id, materials


class TestUsingAScenePicture:
    """封面本该是片子里的一个画面——那些图已经在磁盘上了。"""

    def test_a_scene_picture_becomes_the_thumbnail_at_the_video_s_shape(
        self, task_root
    ):
        task_id, _ = _task_with_real_pictures(task_root, aspect="9:16")
        data = publish_metadata.use_scene(task_id, 1)
        assert (data["thumbnail_width"], data["thumbnail_height"]) == (1080, 1920)
        with Image.open(publish_metadata.thumbnail_file(task_id)) as saved:
            assert saved.size == (1080, 1920)

    def test_a_widescreen_video_crops_the_same_picture_to_a_wide_thumbnail(
        self, task_root
    ):
        task_id, _ = _task_with_real_pictures(task_root, aspect="16:9")
        data = publish_metadata.use_scene(task_id, 0)
        assert (data["thumbnail_width"], data["thumbnail_height"]) == (1280, 720)

    def test_it_takes_the_picture_that_was_chosen(self, task_root):
        """裁出来的必须是点中的那一张，不是第一张。"""
        task_id, materials = _task_with_real_pictures(task_root)
        publish_metadata.use_scene(task_id, 2)
        with Image.open(publish_metadata.thumbnail_file(task_id)) as saved:
            chosen = saved.convert("RGB").getpixel((540, 960))
        with Image.open(materials[2]) as source:
            expected = source.convert("RGB").getpixel((450, 800))
        assert chosen == expected

    def test_the_panel_remembers_which_scene_it_came_from(self, task_root):
        """用户下一步想换的时候，这是唯一要知道的事。"""
        task_id, _ = _task_with_real_pictures(task_root)
        data = publish_metadata.use_scene(task_id, 1)
        assert data["thumbnail_from"] == "scene"
        assert data["thumbnail_scene"] == 1
        assert publish_metadata.describe(task_id)["thumbnail_scene"] == 1

    def test_it_needs_no_image_service_at_all(self, task_root):
        """这正是它比重新画一张好的地方：不用出图，也就不会出图失败。"""
        task_id, _ = _task_with_real_pictures(task_root)
        with patch.dict(
            material.config.app,
            {"openai_image_base_url": "", "openai_image_model": ""},
            clear=False,
        ):
            with patch.object(material, "_request_openai_image") as request:
                data = publish_metadata.use_scene(task_id, 0)
        request.assert_not_called()
        assert data["thumbnail"] == publish_metadata.THUMBNAIL_FILENAME

    def test_drawing_after_picking_replaces_it_and_says_so(self, task_root):
        task_id, _ = _task_with_real_pictures(task_root)
        publish_metadata.use_scene(task_id, 0)
        with patch.dict(
            material.config.app,
            {"openai_image_base_url": "http://local", "openai_image_model": "model"},
            clear=False,
        ):
            with patch.object(
                material, "_request_openai_image", return_value=(_png(), "")
            ):
                data = publish_metadata.generate_thumbnail(task_id, prompt="a seed")
        assert data["thumbnail_from"] == "drawn"
        assert data["thumbnail_scene"] is None

    def test_a_scene_that_does_not_exist_names_the_count(self, task_root):
        task_id, _ = _task_with_real_pictures(task_root, scenes=3)
        with pytest.raises(publish_metadata.SceneError) as error:
            publish_metadata.use_scene(task_id, 9)
        assert "has 3 scenes" in str(error.value)

    def test_a_non_numeric_scene_is_refused(self, task_root):
        task_id, _ = _task_with_real_pictures(task_root)
        with pytest.raises(publish_metadata.SceneError):
            publish_metadata.use_scene(task_id, "first")

    def test_a_picture_missing_from_disk_is_a_named_error(self, task_root):
        task_id, materials = _task_with_real_pictures(task_root)
        os.remove(materials[0])
        with pytest.raises(publish_metadata.SceneError) as error:
            publish_metadata.use_scene(task_id, 0)
        assert "missing" in str(error.value)

    def test_the_panel_is_given_the_pictures_to_choose_from(self, task_root):
        task_id, _ = _task_with_real_pictures(task_root, scenes=3)
        scenes = publish_metadata.describe(task_id)["scenes"]
        assert [scene["index"] for scene in scenes] == [0, 1, 2]
        assert all(scene["url"].startswith("/api/v1/stream/") for scene in scenes)
        assert scenes[1]["keyword"] == "keyword 1"


class TestTakingAFrameOutOfAClip:
    """素材多半是 mp4：图片在出图那一刻就烘成了带运镜的片段。"""

    def _clip(self, duration=3.0):
        import numpy as np

        clip = type("Clip", (), {})()
        clip.duration = duration
        clip.asked = []

        def get_frame(moment):
            clip.asked.append(moment)
            return np.full((1600, 900, 3), 120, dtype="uint8")

        clip.get_frame = get_frame
        return clip

    def _patched(self, clip):
        from app.services import video as video_service

        return patch.multiple(
            video_service,
            _open_video_clip_quietly=lambda path: clip,
            close_clip=lambda handle: None,
        )

    def test_the_first_frame_is_the_default(self, task_root):
        """zoom 是从 1.0 倍往里推的，所以第一帧是这张图最完整的样子。"""
        task_id, _ = _task_with_real_pictures(task_root)
        scenes = scene_manifest.load_raw(task_id)["scenes"]
        scenes[0]["material"] = str(task_root / task_id / "clip.mp4")
        scenes[0]["kind"] = "video"
        (task_root / task_id / "clip.mp4").write_bytes(b"not really an mp4")
        payload = scene_manifest.load_raw(task_id)
        payload["scenes"] = scenes
        task_artifacts.write_script_data(task_id, payload)

        clip = self._clip()
        with self._patched(clip):
            publish_metadata.use_scene(task_id, 0)
        assert clip.asked == [0.0]
        with Image.open(publish_metadata.thumbnail_file(task_id)) as saved:
            assert saved.size == (1080, 1920)

    def test_a_time_past_the_end_is_pulled_back_inside_the_clip(self, task_root):
        """超出末尾会读到空帧，那是一张全黑的封面——看起来就像功能坏了。"""
        task_id, _ = _task_with_real_pictures(task_root)
        scenes = scene_manifest.load_raw(task_id)["scenes"]
        scenes[0]["material"] = str(task_root / task_id / "clip.mp4")
        scenes[0]["kind"] = "video"
        (task_root / task_id / "clip.mp4").write_bytes(b"not really an mp4")
        payload = scene_manifest.load_raw(task_id)
        payload["scenes"] = scenes
        task_artifacts.write_script_data(task_id, payload)

        clip = self._clip(duration=3.0)
        with self._patched(clip):
            publish_metadata.use_scene(task_id, 0, at=99.0)
        assert clip.asked == [pytest.approx(2.95)]


class TestWordsOnTheThumbnail:
    """图和字是两步，代价差了几个数量级。"""

    def _with_picture(self, task_root, aspect="9:16"):
        task_id, _ = _task_with_real_pictures(task_root, aspect=aspect)
        publish_metadata.use_scene(task_id, 0)
        return task_id

    def test_the_text_is_burnt_into_the_downloaded_file(self, task_root):
        """预览里有字而文件里没有，是最糟的一种结果。"""
        task_id = self._with_picture(task_root)
        with Image.open(publish_metadata.thumbnail_file(task_id)) as plain:
            blank = plain.convert("RGB").tobytes()
        publish_metadata.set_text(task_id, {"text": "RICE FOUGHT BACK"})
        with Image.open(publish_metadata.thumbnail_file(task_id)) as lettered:
            assert lettered.convert("RGB").tobytes() != blank

    def test_changing_the_words_does_not_touch_the_picture(self, task_root):
        """改一个词不该重跑一次出图，也不该重新取一次帧。"""
        task_id = self._with_picture(task_root)
        base = os.path.join(str(task_root / task_id), publish_metadata.BASE_FILENAME)
        with open(base, "rb") as handle:
            digest = handle.read()
        publish_metadata.set_text(task_id, {"text": "One"})
        publish_metadata.set_text(task_id, {"text": "Two"})
        with open(base, "rb") as handle:
            assert handle.read() == digest

    def test_changing_the_picture_keeps_the_words(self, task_root):
        """调好的文字不该因为换了一张底图就丢掉。"""
        task_id = self._with_picture(task_root)
        publish_metadata.set_text(task_id, {"text": "STILL HERE", "size": 8})
        data = publish_metadata.use_scene(task_id, 2)
        assert data["text"]["text"] == "STILL HERE"
        assert data["text"]["size"] == 8
        assert data["thumbnail_scene"] == 2

    def test_the_words_default_to_the_title(self, task_root):
        """加字这件事，九成就是把标题压上去。"""
        task_id, _ = _task_with_real_pictures(task_root)
        publish_metadata.set_metadata(task_id, title="A Title Worth Reading")
        assert publish_metadata.describe(task_id)["text"]["text"] == (
            "A Title Worth Reading"
        )

    def test_the_font_defaults_to_the_videos_own_caption_font(self, task_root):
        """封面和片子出自同一条片子，同一款字看起来才像一套。"""
        task_id, _ = _task_with_real_pictures(task_root)
        assert publish_metadata.describe(task_id)["text"]["font"] == (
            scene_editor.load_params(task_id).font_name
        )

    def test_emptying_the_words_leaves_a_downloadable_picture(self, task_root):
        """把字删光也得留下一张能下载的封面，而不是不写这个文件。"""
        task_id = self._with_picture(task_root)
        publish_metadata.set_text(task_id, {"text": "Gone in a moment"})
        publish_metadata.set_text(task_id, {"text": ""})
        path = publish_metadata.thumbnail_file(task_id)
        assert os.path.isfile(path)
        with Image.open(path) as saved:
            assert saved.size == (1080, 1920)

    def test_the_words_fit_inside_the_frame(self, task_root):
        """贴底放置时最后一行会被画出画面外——anchor 对的是上伸部顶端。"""
        task_id = self._with_picture(task_root)
        publish_metadata.set_text(
            task_id,
            {
                "text": "Rice did not want to be farmed",
                "position": "bottom",
                "size": 9,
                "band": False,
            },
        )
        with Image.open(publish_metadata.thumbnail_file(task_id)) as saved:
            pixels = saved.convert("RGB")
            # 最后一行若被切掉，紧贴下边缘会留下一排描边的黑；留足边距时
            # 那一行应当落在边距之上。
            bottom = [
                pixels.getpixel((x, pixels.height - 2))
                for x in range(0, pixels.width, 40)
            ]
        assert not all(sum(colour) < 90 for colour in bottom)

    def test_words_on_a_video_with_no_picture_are_saved_for_later(self, task_root):
        """用户完全可能先写字再挑图。"""
        task_id, _ = _task_with_real_pictures(task_root)
        data = publish_metadata.set_text(task_id, {"text": "Written first"})
        assert data["text"]["text"] == "Written first"
        assert data["has_picture"] is False
        assert data["thumbnail"] == ""

    def test_an_unknown_font_is_refused(self, task_root):
        task_id = self._with_picture(task_root)
        with pytest.raises(publish_metadata.SceneError) as error:
            publish_metadata.set_text(task_id, {"text": "x", "font": "Comic Sans.ttf"})
        assert "unknown font" in str(error.value)

    def test_a_size_outside_the_range_is_refused_not_clamped(self, task_root):
        """界面显示的值和画出来的必须是同一个。"""
        task_id = self._with_picture(task_root)
        with pytest.raises(publish_metadata.SceneError) as error:
            publish_metadata.set_text(task_id, {"text": "x", "size": 99})
        assert "between 3 and 30" in str(error.value)

    def test_a_bad_colour_is_refused(self, task_root):
        task_id = self._with_picture(task_root)
        with pytest.raises(publish_metadata.SceneError) as error:
            publish_metadata.set_text(task_id, {"text": "x", "color": "white"})
        assert "#RRGGBB" in str(error.value)

    def test_text_far_too_long_for_a_thumbnail_is_refused(self, task_root):
        task_id = self._with_picture(task_root)
        with pytest.raises(publish_metadata.SceneError) as error:
            publish_metadata.set_text(task_id, {"text": "word " * 40})
        assert "read in half a second" in str(error.value)

    def test_a_thumbnail_from_before_this_feature_is_adopted_as_the_picture(
        self, task_root
    ):
        """压字是后加的，磁盘上存在有成品、没有 base 的封面。"""
        task_id = self._with_picture(task_root)
        base = os.path.join(str(task_root / task_id), publish_metadata.BASE_FILENAME)
        os.remove(base)
        assert publish_metadata.describe(task_id)["has_picture"] is True
        publish_metadata.set_text(task_id, {"text": "Adopted"})
        assert os.path.isfile(base)

    def test_the_panel_is_given_the_fonts_and_positions(self, task_root):
        task_id, _ = _task_with_real_pictures(task_root)
        data = publish_metadata.describe(task_id)
        assert "MicrosoftYaHeiBold.ttc" in data["text_fonts"]
        assert data["text_positions"] == ["top", "middle", "bottom"]


class TestLetteringItself:
    """文字渲染本身，不经过任务。"""

    def _font(self, pixels=80):
        from PIL import ImageFont

        return ImageFont.truetype(
            os.path.join(utils.font_dir(), "BeVietnamPro-Bold.ttf"), pixels
        )

    def _picture(self, tmp_path, size=(1080, 1920)):
        path = tmp_path / "base.png"
        Image.new("RGB", size, (30, 90, 160)).save(path)
        return str(path)

    def test_wrapping_uses_real_glyph_widths(self):
        """按字数折行对 WWW 和 ill 会差出好几倍。"""
        font = self._font()
        narrow = thumbnail_text._wrap("ill ill ill ill", font, 400)
        wide = thumbnail_text._wrap("WWW WWW WWW WWW", font, 400)
        assert len(wide) > len(narrow)

    def test_a_single_word_wider_than_the_frame_is_not_broken_apart(self):
        """拆开的半个词比探出边界更难读。"""
        assert thumbnail_text._wrap("Supercalifragilistic", self._font(), 50) == [
            "Supercalifragilistic"
        ]

    def test_a_missing_font_is_named_and_not_silently_replaced(self, tmp_path):
        """退回 PIL 的默认位图字体会得到几个像素高的字，比报错更难查。"""
        base = self._picture(tmp_path)
        with pytest.raises(thumbnail_text.ThumbnailTextError) as error:
            thumbnail_text.render(
                base,
                str(tmp_path / "out.png"),
                thumbnail_text.normalise({"text": "hi", "font": "nope.ttf"}),
            )
        assert "nope.ttf" in str(error.value)

    def test_rendering_without_a_picture_is_a_named_error(self, tmp_path):
        with pytest.raises(thumbnail_text.ThumbnailTextError) as error:
            thumbnail_text.render(
                str(tmp_path / "gone.png"), str(tmp_path / "out.png"), None
            )
        assert "no picture" in str(error.value)

    def test_a_failed_render_leaves_the_previous_thumbnail_alone(self, tmp_path):
        """成品是固定文件名，一次失败不该把上一张还能用的封面毁掉。"""
        base = self._picture(tmp_path)
        target = tmp_path / "out.png"
        thumbnail_text.render(
            base,
            str(target),
            thumbnail_text.normalise(
                {"text": "good", "font": "BeVietnamPro-Bold.ttf"}
            ),
        )
        kept = target.read_bytes()
        with pytest.raises(thumbnail_text.ThumbnailTextError):
            thumbnail_text.render(
                base,
                str(target),
                thumbnail_text.normalise({"text": "bad", "font": "missing.ttf"}),
            )
        assert target.read_bytes() == kept
        assert not list(tmp_path.glob("*.part"))
