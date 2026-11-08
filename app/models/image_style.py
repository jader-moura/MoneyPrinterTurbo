"""图片风格预设：一组可直接套用的 ``openai_image_prompt_template``。

Why this exists as presets rather than a prompt box alone
---------------------------------------------------------
``openai_image_prompt_template`` already shapes every generated picture, and it
has always been editable. But it is a free-text field behind a settings dialog,
so choosing a look means knowing which words a diffusion model responds to.
These presets are the same mechanism with the prompt-writing done once.

Why the style has to live in the *positive* prompt
--------------------------------------------------
Z-Image-Turbo is distilled and runs at guidance 0, which switches
classifier-free guidance off and makes a negative prompt do nothing at all. So
"not a photograph" is not expressible; the only way to move the look is to say
what it *is*, in the prompt itself. A template is exactly the right shape for
that, and it works the same way for any other OpenAI-compatible endpoint.

Each template must contain ``{term}``. ``app.services.material._openai_image_prompt``
substitutes the script keyword there, and falls back to the bare keyword when a
template has no placeholder — a preset without one would silently generate the
same picture for every term.
"""

from dataclasses import dataclass


# “自定义”不是预设，而是“模板由用户自己写”的哨兵值。它不会写入 config.toml：
# 配置里保存的始终是模板文本本身,风格只是读取模板后反推出来的展示状态。
# Not a preset: the sentinel for "the template is whatever the user typed".
# It is never written to config.toml — the config always holds the template
# text, and the selected style is derived back from it.
IMAGE_STYLE_CUSTOM = "custom"

# 只有按关键词作图的来源才谈得上风格；local 用的是用户自己的文件。
# Only sources that *generate* from a keyword have a style to choose. Local
# materials are the user's own files, so the template never reaches them.
IMAGE_STYLE_SOURCES = ("openai_image", "codex_image")


@dataclass(frozen=True, slots=True)
class ImageStyle:
    """一个风格预设：稳定 id、locale key 与提示词模板。"""

    style_id: str
    label_key: str
    template: str


# 顺序即 WebUI 展示顺序：先放最常用的写实类，再到插画与绘画类。
# The order here is the order in the selector: photographic looks first,
# because they suit the documentary-style scripts the app writes by default,
# then illustration and painting.
#
# 第一个必须与 config.example.toml 里的默认模板逐字相同，否则默认安装打开
# WebUI 时会显示“自定义”。test_image_style.py 对此有断言。
# The first entry must match the shipped default template verbatim, or a fresh
# install would open the selector showing "Custom". Asserted in the tests.
#
# “stickman”是唯一把 {term} 放在开头的预设,这不是笔误：它的风格描述很长,
# 把场景留到最后时模型会忽略动作——实测“农民播种”只画出一个站在空地上的
# 人,既没有种子也没有道具。把 {term} 提前后,同一个关键词画出了弯腰栽种
# 幼苗、旁边还有一排土坑的画面。
# "stickman" is the one preset that leads with {term}, and that is deliberate:
# its style description is long, and with the scene left until the end the
# model dropped the action entirely. Measured, not guessed.
IMAGE_STYLE_PRESETS = (
    ImageStyle(
        "photorealistic",
        "Image Style Photorealistic",
        "{term}, candid cinematic photograph, natural light, documentary style,"
        " 35mm film, highly detailed, sharp focus",
    ),
    ImageStyle(
        "cinematic",
        "Image Style Cinematic",
        "{term}, cinematic film still, dramatic directional lighting, shallow"
        " depth of field, anamorphic lens, colour graded, highly detailed",
    ),
    ImageStyle(
        "monochrome",
        "Image Style Monochrome",
        "{term}, black and white photograph, high contrast, grainy 35mm film,"
        " photojournalism, strong shadows",
    ),
    ImageStyle(
        "vintage_film",
        "Image Style Vintage Film",
        "{term}, vintage colour photograph, 1970s Kodachrome, soft grain, faded"
        " warm tones, period accurate clothing and objects",
    ),
    ImageStyle(
        "anime",
        "Image Style Anime",
        "{term}, anime illustration, cel shaded, vibrant colours, clean"
        " linework, studio animation key visual",
    ),
    ImageStyle(
        "digital_art",
        "Image Style Digital Art",
        "{term}, digital illustration, concept art, painterly brushwork, rich"
        " colour, dramatic composition, highly detailed",
    ),
    ImageStyle(
        "oil_painting",
        "Image Style Oil Painting",
        "{term}, oil painting on canvas, visible brush strokes, classical"
        " composition, warm palette, fine art",
    ),
    ImageStyle(
        "watercolour",
        "Image Style Watercolour",
        "{term}, watercolour painting, soft washes of pigment, textured paper,"
        " delicate linework, light and airy",
    ),
    ImageStyle(
        "three_d",
        "Image Style 3D Render",
        "{term}, 3D render, physically based materials, soft global"
        " illumination, subtle depth of field, high detail",
    ),
    ImageStyle(
        "flat_vector",
        "Image Style Flat Vector",
        "{term}, flat vector illustration, bold simple shapes, limited colour"
        " palette, clean negative space, editorial style",
    ),
    ImageStyle(
        "stickman",
        "Image Style Stickman",
        "{term}. Drawn as a simple 2D stickman cartoon illustration in flat"
        " vector style: one stick figure character with a large perfectly round"
        " white head, a thin black outline, dot eyes, a small curved mouth and"
        " emotive eyebrows, and thin clean black lines of even weight for arms"
        " and legs. The character holds or wears one simple prop that shows its"
        " role, and is clearly performing the action. Bright cheerful saturated"
        " flat colour fills, crisp solid shapes, minimal soft shadow."
        " Uncluttered background of a few simple flat shapes that set the"
        " scene, with generous clean empty space. Centred, clearly readable,"
        " lighthearted humorous educational explainer look, 16:9 widescreen.",
    ),
    ImageStyle(
        "comic",
        "Image Style Comic",
        "{term}, comic book art, bold ink outlines, halftone shading, dynamic"
        " composition, saturated colour",
    ),
)

IMAGE_STYLES = {style.style_id: style for style in IMAGE_STYLE_PRESETS}

# 默认模板来自第一个预设，避免默认值在常量和配置文件之间各写一份。
# The shipped default comes from the first preset, so the string is not
# maintained separately in a constant and in the example configs.
DEFAULT_IMAGE_STYLE_ID = IMAGE_STYLE_PRESETS[0].style_id
DEFAULT_IMAGE_PROMPT_TEMPLATE = IMAGE_STYLE_PRESETS[0].template


def image_style_ids() -> tuple:
    """返回预设 id，顺序与展示顺序一致，不含“自定义”。"""
    return tuple(style.style_id for style in IMAGE_STYLE_PRESETS)


def get_image_style(style_id):
    """按 id 取预设；未知 id 返回 ``None``，由调用方决定如何提示。"""
    return IMAGE_STYLES.get(str(style_id or "").strip().lower())


def template_for_style(style_id) -> str:
    """
    返回该风格的模板文本。

    “自定义”和未知 id 都返回空字符串：两者都表示“不要由预设覆盖用户模板”。
    """
    style = get_image_style(style_id)
    return style.template if style is not None else ""


def style_for_template(template) -> str:
    """
    从模板文本反推当前风格,匹配不上时返回“自定义”。

    反推而不是另存一个 ``ui.image_style``,是为了只有一个真相来源：用户直接
    编辑模板后,选择器会立刻显示“自定义”,而不是继续显示一个早已不生效的
    预设名。比较时忽略首尾空白,因为配置写回时会 strip。
    """
    text = str(template or "").strip()
    if not text:
        return IMAGE_STYLE_CUSTOM
    for style in IMAGE_STYLE_PRESETS:
        if style.template == text:
            return style.style_id
    return IMAGE_STYLE_CUSTOM
