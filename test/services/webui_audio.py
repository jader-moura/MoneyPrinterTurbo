"""页面上的音频播放器：按内容认人，而不是数个数。

Why this exists
---------------
这一页上能同时出现三种播放器：配音试听、背景音乐试听、转场音效试听。
"整页只有一个播放器"这种断言实际上是在要求这一页永远不许再长出别的声音
——转场音效上线后，只要跑测试的那台机器在自己的 ``config.toml`` 里选了一个
音效，一批与配音和背景音乐有关的测试就会一起失败，而它们测的东西根本没坏。
一个断言不该取决于开发者在界面上点过什么。

Streamlit 的媒体地址是按内容算出来的（``/mock/media/<内容哈希>.<扩展名>``），
所以"这段音频的播放器"可以按字节认出来：既不依赖它在页面上的位置，也不受
页面上别的播放器影响。

The page can hold three unrelated players: the voice preview, the background
music preview and the transition-sound preview. Asserting on the whole page's
player count therefore asserts on the developer's own saved settings. Streamlit
addresses media by content, so each player can be identified by the bytes it
plays instead.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

# 这就是 Streamlit 自己算媒体 id 的那个函数。复制一份哈希实现会在它换算法时
# 悄悄算错；直接用它的话，万一改了名字，测试会立刻以 ImportError 失败,那是
# 看得见的失败。
from streamlit.runtime.memory_media_file_storage import _calculate_file_id

from app.config import config
from app.services import transition_sound as transition_sound_service


def players_for_bytes(app, data: bytes, mimetype: str) -> list:
    """页面上正在播这段字节的播放器。"""
    if not data:
        return []
    file_id = _calculate_file_id(data, mimetype)
    return [item for item in app.get("audio") if file_id in item.proto.url]


def players_for_file(app, path) -> list:
    """同上，音频来自一个文件。

    ``mimetype`` 按 ``webui/Main.py`` 里同一条规则推断，否则算出来的 id 和
    页面上的对不上。
    """
    mimetype = mimetypes.guess_type(str(path))[0] or "audio/mpeg"
    try:
        data = Path(path).read_bytes()
    except OSError:
        # 文件不存在时页面本来就放不出播放器，这与"没找到"是同一个答案。
        return []
    return players_for_bytes(app, data, mimetype)


def voice_preview_players(app) -> list:
    """配音试听的播放器。页面把试听音频缓存在 session 里，这里照它来找。"""
    try:
        cached = app.session_state["voice_preview_audio"] or {}
    except (KeyError, AttributeError):
        return []
    return players_for_bytes(
        app,
        cached.get("audio_bytes") or b"",
        cached.get("mime_type", "audio/mp3"),
    )


def transition_sound_players(app) -> list:
    """转场音效试听的播放器。

    放不放完全取决于跑测试的这台机器选了哪个音效（``ui.transition_sound``），
    所以凡是要断言"没有出现播放器"的地方都得先把它排除掉。
    """
    name = str(config.ui.get("transition_sound", "") or "").strip()
    if not name:
        return []
    try:
        return players_for_file(
            app, transition_sound_service.resolve_transition_sound(name)
        )
    except ValueError:
        # 配置里写着一个已经不在音效库里的名字，那页面上也没有这个播放器。
        return []


def bgm_players(app) -> list:
    """背景音乐面板上的播放器。

    用减法：整页的播放器减去不属于这个面板的那些。背景音乐的音频既可能是
    预设歌曲文件，也可能是用户刚上传的那个 ``UploadedFile``，两者的字节不在
    测试的掌握之内（比如文件在枚举后消失的用例），所以这里不去正面认它。
    """
    others = {
        id(item)
        for item in (*voice_preview_players(app), *transition_sound_players(app))
    }
    return [item for item in app.get("audio") if id(item) not in others]
