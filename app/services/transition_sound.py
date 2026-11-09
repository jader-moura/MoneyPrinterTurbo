"""转场音效库：列出可选音效、按文件名解析路径。

背景音乐铺满整条片子，转场音效只在每两段画面的交界处响一下，所以两者各有
自己的目录和自己的音量：``resource/transitions`` 是随项目分发的内置音效，
``storage/transitions`` 是用户自己丢进去的（storage 在 Docker 里是挂载卷，
重建容器后还在，也不会污染 Git 工作区——和用户上传的背景音乐同一个道理）。

A transition sound is a short whoosh or pop played at each cut. Picking one is
optional: an empty name keeps the video exactly as it was before this feature.
"""

import math
import os
from pathlib import Path

from loguru import logger

from app.services import bgm as bgm_service
from app.utils import file_security, utils


# 解码交给 FFmpeg，所以支持的扩展名与背景音乐完全一致，没有理由在这里维护
# 第二份清单——用户从同一个素材站下载的 wav 音效不该因为目录不同而被拒绝。
SUPPORTED_TRANSITION_SOUND_EXTENSIONS = bgm_service.SUPPORTED_BGM_EXTENSIONS

# 音效文件的播放上限。素材站的“转场音效”里混着几十秒的环境音（水声、电流声），
# 整段铺在剪辑点上会盖掉旁白，而用户选它多半只是想要开头那一下。超过这个长度
# 的文件只播开头，结尾由 ``TRANSITION_SOUND_FADE_SECONDS`` 淡出，不会有硬切。
MAX_TRANSITION_SOUND_SECONDS = 2.0
TRANSITION_SOUND_FADE_SECONDS = 0.25


def _library_directories() -> tuple[str, ...]:
    """内置音效在前，用户目录在后；重名时用户文件优先（见 ``_list_sounds``）。"""
    return (utils.transition_sound_dir(), user_transition_sound_dir(create=True))


def user_transition_sound_dir(create: bool = True) -> str:
    """用户自己放转场音效的目录。"""
    return utils.storage_dir("transitions", create=create)


def _list_sounds(directories: tuple[str, ...]) -> list[str]:
    """按目录顺序枚举安全且受支持的音效文件，后面的目录覆盖同名文件。"""
    files_by_name: dict[str, str] = {}
    for directory in directories:
        if not os.path.isdir(directory):
            continue
        for name in sorted(os.listdir(directory), key=str.lower):
            if Path(name).suffix.lower() not in SUPPORTED_TRANSITION_SOUND_EXTENSIONS:
                continue
            try:
                # 枚举结果也要做真实路径校验：否则在允许目录里放一个指向外部
                # 文件的音频符号链接，就能借这条路径把任意文件交给 MoviePy。
                resolved_path = file_security.resolve_path_within_directory(
                    directory, os.path.join(directory, name)
                )
            except ValueError as exc:
                logger.warning(
                    f"skip unsafe transition sound file: name={name}, error={str(exc)}"
                )
                continue
            files_by_name[name] = resolved_path
    return [files_by_name[name] for name in sorted(files_by_name, key=str.lower)]


def list_transition_sounds() -> list[str]:
    """列出全部可选转场音效的真实路径。"""
    return _list_sounds(_library_directories())


def list_builtin_transition_sounds() -> list[str]:
    """只列出随项目分发的音效。

    设置预设要能在另一台装了同一版本的机器上还原，所以导出导入只认内置音效，
    与背景音乐的“预设歌曲”同一条规则。
    """
    return _list_sounds((utils.transition_sound_dir(),))


def resolve_transition_sound(unsafe_name: str) -> str:
    """按文件名解析转场音效，拒绝路径分隔符和音效库之外的任何位置。"""
    if not unsafe_name:
        raise ValueError("transition sound filename is required")

    filename = str(unsafe_name)
    if filename != os.path.basename(filename):
        raise ValueError("transition sound must be given as a filename")

    files_by_name = {
        os.path.basename(file_path): file_path
        for file_path in list_transition_sounds()
    }
    if filename not in files_by_name:
        raise ValueError("transition sound is not available")
    return files_by_name[filename]


def should_use_transition_sound(name: str | None, volume: float | None) -> bool:
    """没选音效、音量非法或音量不大于 0 时，整条音效链路都要跳过。

    与 ``bgm.should_use_bgm`` 同样的判断：把它放在一个地方，渲染、WebUI 和
    任务编排就不会各写一套 0 音量判断，也不会出现界面显示有音效、成片里没有。
    """
    if not str(name or "").strip():
        return False
    try:
        normalized_volume = float(volume or 0)
    except (TypeError, ValueError):
        return False
    return math.isfinite(normalized_volume) and normalized_volume > 0
