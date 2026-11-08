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
# “stickman”的措辞是逐张比对出来的,不要随手改长。实测十余张图说明：模型
# 画这类角色时有很强的固有偏好,再怎么写“细黑线四肢”也只会得到圆头白身的
# 吉祥物;真正会丢的是动作和道具。所以“手里拿着与动作相关的物体、充满动感
# 的姿势”放在前面,背景描述放在最后——把词都花在背景上的那一版画出了漂亮的
# 风景和一个呆站着的角色。另外“whiteboard”不能用,模型会真的画一块白板。
# The wording here was arrived at by comparing frames, not by writing prose.
# The test images showed the model has a strong prior for this round-headed
# mascot and ignores limb geometry however it is described -- what it actually
# drops is the action and the prop. So the action clause comes first and the
# background clause last: the variant that spent its words on scenery drew a
# beautiful landscape with a figure standing in it doing nothing. "objects of
# the action" beats "tools of the action", which biases towards implements and
# got a spatula for a keyword about two bottles. "whiteboard" is unusable --
# the model draws an actual whiteboard, frame and all.
#
# 两个已知局限,不必再去调措辞：一是“贴地”的动作(弯腰、蹲下、在地面操作)
# 大约一半的出图会丢掉道具,换过的每一种写法都这样,是模型本身的问题;二是
# “expressive eyebrows”偶尔会画成皱眉,因为斜眉在模型眼里就是生气。想改表
# 情只能连带牺牲别的,实测把它换成“与内容相称的表情”后,“存钱”这个词画出了
# 一张哭丧脸。
# Two known limits, so nobody burns another afternoon on the wording. Actions
# at ground level -- bending, crouching, working on the floor -- drop the prop
# in roughly half the draws, and did so in every wording tried; that is the
# model, not the prompt. And "expressive eyebrows" occasionally lands as a
# frown, because angled brows read as anger. Replacing it with "an expression
# that matches what is happening" was tested and is worse: it drew a
# miserable face for a keyword about saving money.
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
        "{term}, cute stick figure cartoon, round white head, dot eyes,"
        " expressive eyebrows, big happy smile, thin black line arms and legs,"
        " clearly holding the objects of the action, mid-action pose full of"
        " energy, bright flat vector colours, simple flat background showing"
        " where this happens, full body centred, clean uncluttered composition,"
        " wide 16:9 explainer illustration",
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
