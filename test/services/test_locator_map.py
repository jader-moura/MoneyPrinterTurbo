"""定位地图：告诉观众"这是在哪儿"的那一张图。

为什么要这些测试
----------------
地图是照经纬度画的，而经纬度是写分镜的人（或者模型）凭记忆写的——写错一位
小数，红点就落在海里，而且一张深色的地图上谁也看不出来。所以这里钉住：

* 红点落在它该在的像素上，陆地是陆地、海是海；
* 写错的地图在读分镜的时候就被拒掉，说是第几段、错在哪；
* 报告里用数据把"这个坐标是哪儿"说一遍，让人能核对；
* 地图是一张普通的画面：放进一段之后，原来那张图还在历史里，再放一次什么
  都不做，"开画"会跳过它。

测试用的是几行字的假数据（一个方形的国家、一个镇），不依赖下载的那 27 MB。

A locator map is drawn from coordinates someone typed from memory, so these
pin where the marker lands, that a wrong map is refused when the plan is read,
that the report says where the coordinates really are, and that a map behaves
like any other scene picture.
"""

import json

import pytest

from app.services import direction_preview, locator_map, scene_editor, scene_manifest
from app.services import scene_plan, task_artifacts
from test.services.test_narration_pauses import LINES, _task, task_root  # noqa: F401


def _square(west, south, east, north):
    return [[[west, south], [east, south], [east, north], [west, north], [west, south]]]


@pytest.fixture
def world(tmp_path, monkeypatch):
    """一个小世界：赤道上一个 10°×10° 的方形国家，里面一个镇，一条国界。"""
    folder = tmp_path / "map_data"
    folder.mkdir()

    def write(kind, features):
        (folder / locator_map.FILES[kind]).write_text(
            json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")

    land = {"type": "Polygon", "coordinates": _square(0, 0, 10, 10)}
    write("land", [{"type": "Feature", "properties": {}, "geometry": land}])
    write("islands", [])
    write("lakes", [])
    write("countries", [{"type": "Feature", "geometry": land, "properties": {
        "NAME": "Testland", "LABEL_X": 5.0, "LABEL_Y": 8.0, "POP_EST": 5000000}}])
    write("borders", [{"type": "Feature", "properties": {}, "geometry": {
        "type": "LineString", "coordinates": [[5, 0], [5, 10]]}}])
    write("states", [])
    write("places", [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [5, 5]},
                      "properties": {"name": "Middleton", "sov0name": "Testland"}}])
    monkeypatch.setattr(locator_map, "data_dir", lambda: str(folder))
    for cached in (locator_map._polygons, locator_map._lines, locator_map._countries,
                   locator_map._places):
        cached.cache_clear()
    yield folder
    for cached in (locator_map._polygons, locator_map._lines, locator_map._countries,
                   locator_map._places):
        cached.cache_clear()


PLACE = {"lat": 5.0, "lon": 5.0, "label": "Middleton", "width_km": 2000}


class TestWhatAPlanMayAskFor:
    def test_the_defaults_are_filled_in(self):
        found = locator_map.checked({"lat": "35.889", "lon": -75.661, "label": " Roanoke  Island "})
        assert found == {
            "lat": 35.889, "lon": -75.661, "label": "Roanoke Island", "sublabel": "",
            "width_km": locator_map.DEFAULT_WIDTH_KM, "modern": True, "also": [],
        }

    @pytest.mark.parametrize("change, said", [
        ({"lat": 95}, "lat is 95"),
        ({"lon": -200}, "lon is -200"),
        ({"lat": "north"}, "needs a number for 'lat'"),
        ({"label": ""}, "has no label"),
        ({"label": "x" * 41}, "41 characters"),
        ({"width_km": 5}, "width_km is 5"),
        ({"also": [{"lat": 1}]}, "other place needs a number for 'lon'"),
        ({"also": [{}] * 7}, "at most 6"),
    ])
    def test_what_is_wrong_is_said_with_the_scene_number(self, change, said):
        with pytest.raises(locator_map.MapError, match=said) as raised:
            locator_map.checked({**PLACE, **change}, 12)
        assert str(raised.value).startswith("scene 12: ")

    def test_the_zoom_is_a_named_width_and_a_number_wins_over_it(self):
        assert locator_map.checked({**PLACE, "width_km": None, "zoom": "Area"})["width_km"] == 400
        assert locator_map.checked({**PLACE, "zoom": "world", "width_km": 250})["width_km"] == 250
        assert sorted(locator_map.ZOOMS.values()) == list(locator_map.ZOOMS.values())
        with pytest.raises(locator_map.MapError, match="zoom is 'street'; it is one of city, area"):
            locator_map.checked({**PLACE, "zoom": "street"}, 3)

    def test_modern_borders_can_be_turned_off(self):
        assert locator_map.checked({**PLACE, "modern": False})["modern"] is False


class TestWhereThatReallyIs:
    def test_a_point_on_land_names_the_country_and_the_nearest_town(self, world):
        assert locator_map.where_is(5.0, 6.0) == "in Testland, 111 km from Middleton"

    def test_a_point_typed_wrong_shows_up_as_being_at_sea(self, world):
        said = locator_map.where_is(5.0, 30.0)
        assert said.startswith("at sea") and "km from Middleton (Testland)" in said

    def test_missing_data_names_the_file_and_where_to_get_it(self, world):
        (world / locator_map.FILES["countries"]).unlink()
        with pytest.raises(locator_map.MapError, match="ne_50m_admin_0_countries.geojson") as raised:
            locator_map.where_is(5.0, 5.0)
        assert locator_map.SOURCE in str(raised.value)


class TestThePicture:
    def test_the_marker_is_where_the_place_is_and_land_is_not_sea(self, world):
        image = locator_map.render(PLACE, (960, 540))
        assert image.size == (960, 540)
        # 要标的地方在画面 44% 宽、正中高的位置：那里是红点的白芯，旁边是红的。
        marker_x, marker_y = round(960 * 0.44), 270
        core = image.getpixel((marker_x, marker_y))
        ring = image.getpixel((marker_x - 5, marker_y))
        assert min(core) > 180
        assert ring[0] > 150 and ring[1] < 110 and ring[2] < 110
        # 2000 公里宽的图，一度约 53 像素：往西 3 度还在国内，往西 8 度是海。
        land = image.getpixel((marker_x - 160, marker_y + 120))
        sea = image.getpixel((marker_x - 420, marker_y + 120))
        assert sum(land) > sum(sea) + 40

    def test_without_modern_borders_only_the_coast_is_drawn(self, world):
        # 国界是一条竖线，正好穿过要标的地方；往北 3 度那一点上看它在不在。
        at = (round(960 * 0.44), 270 - 160)
        with_border = locator_map.render(PLACE, (960, 540)).getpixel(at)
        without = locator_map.render({**PLACE, "modern": False}, (960, 540)).getpixel(at)
        assert sum(with_border) > sum(without) + 60

    def test_it_is_saved_whole(self, world, tmp_path):
        target = tmp_path / "map.png"
        assert locator_map.save(PLACE, str(target), (480, 270)) == str(target)
        assert target.read_bytes().startswith(b"\x89PNG")
        assert not list(tmp_path.glob("*.part.png"))


PLAN = {"scenes": [
    {"narration": line, "prompt": "A tent in the snow"} for line in LINES
]}


def _plan(**second):
    plan = json.loads(json.dumps(PLAN))
    plan["scenes"][1].update(second)
    return json.dumps(plan)


class TestAMapInAScenePlan:
    def test_a_scene_with_a_map_needs_no_prompt(self):
        plan = json.loads(_plan(map=PLACE))
        del plan["scenes"][1]["prompt"]
        parsed = scene_plan.parse(json.dumps(plan))
        assert parsed["scenes"][1]["direction"]["map"]["label"] == "Middleton"
        assert "Middleton" in parsed["scenes"][1]["prompt"]
        assert scene_plan.stored(parsed)[1]["direction"]["map"]["width_km"] == 2000

    @pytest.mark.parametrize("second, said", [
        ({"map": {"lat": 5, "lon": 5}}, "scene 2: the map has no label"),
        ({"image": "map"}, 'scene 2 says its image is a map but has no "map"'),
        ({"map": PLACE, "image": "photo", "photo_name": "Igor Dyatlov"},
         "scene 2 is both a map and drawn from a photo"),
    ])
    def test_a_map_that_cannot_be_drawn_is_refused_when_the_plan_is_read(self, second, said):
        with pytest.raises(scene_plan.ScenePlanError, match=said):
            scene_plan.parse(_plan(**second))

    def test_the_report_says_where_the_coordinates_really_are(self, world):
        said = scene_plan.report(scene_plan.parse(_plan(map=PLACE, image="map")))
        assert "map on scene 2" in said.split("direction: ")[1].split("\n")[0]
        assert ("map on scene 2: Middleton at 5, 5 is in Testland, 0 km from Middleton; "
                "the view is 2000 km wide") in said

    def test_the_report_still_comes_out_without_the_map_data(self, world):
        (world / locator_map.FILES["countries"]).unlink()
        said = scene_plan.report(scene_plan.parse(_plan(map=PLACE)))
        assert "map on scene 2: Middleton at 5, 5 is not checked" in said


class TestAMapIsAnOrdinaryScenePicture:
    def test_the_scene_shows_the_map_and_keeps_its_old_picture_in_its_history(
            self, world, task_root):  # noqa: F811
        task_id = _task(task_root)
        before = scene_manifest.load(task_id)["scenes"][1]["material"]
        scene = scene_editor.place_map(task_id, 1, PLACE)
        assert scene["material"] != before and scene["material"].endswith(".mp4")
        assert scene["kind"] == "video" and scene["locked"] is True
        pictures = list((task_root / task_id).glob("map-*.png"))
        assert len(pictures) == 1
        # 片段是从那张地图烘出来的：以后要把这一段拉长，原图找得回来。
        from app.services import timeline
        assert timeline.source_image_for(task_id, scene["material"]) == str(pictures[0])
        stored = scene_manifest.load(task_id)["scenes"][1]
        assert any(before.endswith(str(old.get("material") or "?"))
                   or str(old.get("material")) == before for old in stored.get("history") or [])

    def test_placing_the_same_map_again_does_nothing(self, world, task_root, monkeypatch):  # noqa: F811
        task_id = _task(task_root)
        first = scene_editor.place_map(task_id, 1, PLACE)["material"]
        from app.services import video

        def refuses(*args, **kwargs):
            raise AssertionError("the map was rendered a second time")

        monkeypatch.setattr(video, "render_image_zoom_video", refuses)
        assert scene_editor.place_map(task_id, 1, PLACE)["material"] == first

    def test_a_planned_map_is_placed_before_anything_is_drawn(self, world, task_root):  # noqa: F811
        task_id = _task(task_root)
        scenes = scene_manifest.load(task_id)["scenes"]
        for scene in scenes:
            scene["material"] = ""
        scene_manifest.save_scenes(task_id, scenes)
        planned = [{"narration": line, "photo": None} for line in LINES]
        planned[2]["direction"] = {"map": locator_map.checked(PLACE)}
        assert scene_editor.place_planned_maps(task_id, planned) == 1
        waiting = scene_manifest.pending_scenes(scene_manifest.load(task_id)["scenes"])
        assert [scene["narration"] for scene in waiting] == [LINES[0], LINES[1], LINES[3]]

    def test_a_map_that_cannot_be_drawn_leaves_the_scene_for_the_picture_model(
            self, world, task_root):  # noqa: F811
        task_id = _task(task_root)
        (world / locator_map.FILES["land"]).unlink()
        planned = [{"narration": line, "photo": None} for line in LINES]
        planned[0]["direction"] = {"map": locator_map.checked({**PLACE, "label": "Elsewhere"})}
        before = scene_manifest.load(task_id)["scenes"][0]["material"]
        assert scene_editor.place_planned_maps(task_id, planned) == 0
        assert scene_manifest.load(task_id)["scenes"][0]["material"] == before


class TestAddingAMapToAVideoThatExists:
    def test_the_direction_file_places_it_on_the_scene_that_says_the_line(
            self, world, task_root):  # noqa: F811
        task_id = _task(task_root)
        said = direction_preview.redirect(task_id, json.dumps({"scenes": {"3": {"map": PLACE}}}))
        assert "scene 3 now shows a map of Middleton" in said
        assert "map on scene 3: Middleton at 5, 5 is in Testland" in said
        assert scene_manifest.load(task_id)["scenes"][2]["material"].endswith(".mp4")
        plan = task_artifacts.read_script_data(task_id)["params"]["scene_plan"]
        assert plan[2]["direction"]["map"]["label"] == "Middleton"

    def test_taking_it_out_of_the_file_says_how_to_get_the_picture_back(
            self, world, task_root):  # noqa: F811
        task_id = _task(task_root)
        direction_preview.redirect(task_id, json.dumps({"scenes": {"3": {"map": PLACE}}}))
        placed = scene_manifest.load(task_id)["scenes"][2]["material"]
        said = direction_preview.redirect(task_id, json.dumps({"scenes": {"3": {"impact": "end"}}}))
        assert "scene 3 no longer has a map in the file" in said and "history" in said
        assert scene_manifest.load(task_id)["scenes"][2]["material"] == placed
