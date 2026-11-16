"""新建视频时挑画风：看图挑，不是看名字挑。

"水彩"和"油画"差在哪，名字说不清。表单里那一项从下拉框换成一排卡片，
每张卡片是同一个画面按那种画风画的样图。

钉住的事：

* 服务端把这一项说成 ``styles``，能挑的值和以前一个不差（每种画风的 id，
  加上空串＝照设置里的）；
* 每种画风都有一张样图，而且是小图；
* 页面按卡片画它，点一张就是选了那个值；样图缺了也还能点。

Picking the picture style for a new video from sample pictures instead of a
list of names. The values chosen are exactly what the select offered.
"""

from pathlib import Path

from PIL import Image

from app.models import image_style
from app.services import new_video

from test.services.test_new_video import _fields, saved  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "resource/public/editor"
JS = (PAGE / "editor.js").read_text(encoding="utf-8").replace("\r\n", "\n")
CSS = (PAGE / "editor.css").read_text(encoding="utf-8").replace("\r\n", "\n")


class TestTheForm:
    def test_the_style_is_chosen_from_cards(self, saved):  # noqa: F811
        field = _fields(new_video.form())["image_style"]
        assert field["kind"] == "styles"

    def test_the_choices_are_the_ones_the_select_had(self, saved):  # noqa: F811
        field = _fields(new_video.form())["image_style"]
        assert [one["id"] for one in field["options"]] == (
            [""] + list(image_style.image_style_ids())
        )
        assert field["options"][0]["label"] == "As in settings"

    def test_as_in_settings_knows_which_style_that_is(self, saved):  # noqa: F811
        field = _fields(new_video.form())["image_style"]
        assert field["settings_style"] in ("", *image_style.image_style_ids())


def _samples(style_id):
    return [
        PAGE / "styles" / (f"{style_id}.jpg" if n == 1 else f"{style_id}-{n}.jpg")
        for n in range(1, new_video.STYLE_SAMPLES + 1)
    ]


class TestTheSamplePictures:
    def test_the_form_says_how_many_there_are(self, saved):  # noqa: F811
        field = _fields(new_video.form())["image_style"]
        assert field["samples"] == new_video.STYLE_SAMPLES >= 2

    def test_every_style_has_all_of_them(self):
        for style_id in image_style.image_style_ids():
            for path in _samples(style_id):
                assert path.is_file(), path.name

    def test_they_are_small_and_widescreen(self):
        for style_id in image_style.image_style_ids():
            for path in _samples(style_id):
                with Image.open(path) as picture:
                    assert picture.size == (384, 216), path.name
                assert path.stat().st_size < 80 * 1024, path.name


class TestThePage:
    def test_the_field_is_drawn_as_cards(self):
        field = JS.split("function formField(field, value, changed) {")[1]
        assert 'field.kind === "styles"' in field
        assert "box.appendChild(styleCards(field, value, changed));" in field

    def test_a_card_shows_the_sample_and_the_name(self):
        cards = JS.split("function styleCards(field, value, changed) {")[1].split("\n}\n")[0]
        assert ("showStyleSamples(card, frame, shown, Number(field.samples) || STYLE_SAMPLES);"
                in cards)
        # 页面自己带的张数和服务端说的是同一个数。
        assert f"const STYLE_SAMPLES = {new_video.STYLE_SAMPLES};" in JS
        assert "one.id ? styleLabel(one.id) : one.label" in cards
        assert ('return `styles/${encodeURIComponent(id)}${n > 1 ? "-" + n : ""}.jpg`;'
                in JS)

    def test_hovering_goes_through_the_other_examples_and_leaving_goes_back(self):
        samples = JS.split("function showStyleSamples(card, frame, id, count) {")[1].split("\n}\n")[0]
        assert 'card.addEventListener("mouseenter", start);' in samples
        assert 'card.addEventListener("mouseleave", stop);' in samples
        assert "show(at % count + 1);" in samples
        assert "if (at !== 1) show(1);" in samples
        # 键盘上也一样。
        assert 'card.addEventListener("focus", start);' in samples

    def test_the_cards_sit_two_to_a_row(self):
        cards = CSS.split(".style-cards {")[1].split("}")[0]
        assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in cards

    def test_clicking_a_card_chooses_that_style(self):
        cards = JS.split("function styleCards(field, value, changed) {")[1].split("\n}\n")[0]
        assert 'card.addEventListener("click", () => changed(one.id, true));' in cards

    def test_as_in_settings_shows_the_settings_style(self):
        cards = JS.split("function styleCards(field, value, changed) {")[1].split("\n}\n")[0]
        assert 'const shown = one.id || field.settings_style || "";' in cards

    def test_a_missing_sample_leaves_a_card_that_still_works(self):
        samples = JS.split("function showStyleSamples(card, frame, id, count) {")[1].split("\n}\n")[0]
        assert "if (at === 1) { picture.remove(); dots.remove(); count = 1; }" in samples

    def test_the_chosen_card_is_marked(self):
        assert ".style-card.on { border-color: var(--accent);" in CSS
        assert ".style-cards {" in CSS
