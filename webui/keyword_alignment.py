"""关键词框什么时候正在顶掉"逐句配画"。

关键词框一旦非空，``task.generate_terms`` 里的逐句分支整条就不跑了
(``if not video_terms and align_terms_to_narration``)，可界面上那个"逐句
配画"的勾还好端端地勾着。用户以为两个都在生效，拿到的却是几个关键词轮播
出来的两百张图——几个小时的显卡时间，画的全是片子别处的东西，而且看得出
不对的时候已经画完了。

更阴的是这个框会自己填上：生成文案、套用 loomloom 候选、点"生成关键词"
都会往里写。所以不能指望用户"记得清空"，得由界面自己说出来。

这两个函数单独放在这里而不是写进 ``Main.py``：那是个一导入就把整个
Streamlit 应用跑起来的入口，判断条件这种该被钉死的东西要能脱开界面来测。

Filling the keywords box silently switches off per-line alignment while the
checkbox still claims to be on, and the box fills itself whenever a script is
generated - so "remember to clear it" is not a workflow, the UI has to say it.
These live outside ``Main.py`` because importing that module runs the whole
Streamlit app, and this condition is worth testing on its own.
"""

from app.services import material


def keywords_override_alignment(video_terms, align_terms_to_narration):
    """关键词是否正在顶掉逐句配画。

    判断条件必须和 ``task.generate_terms`` 分支的条件完全一致，否则界面
    迟早会在该报警的时候不报警——而用户为此付的是显卡时间。
    """
    if not align_terms_to_narration:
        # 逐句配画本来就关着，没有什么被顶掉。这时候再弹一条警告只是噪音。
        return False
    if isinstance(video_terms, (list, tuple)):
        return any(str(term or "").strip() for term in video_terms)
    return bool(str(video_terms or "").strip())


def keywords_override_warning_key(video_source):
    """两种素材源吃关键词的方式不同，后果也不同，得分开说。

    文生图把关键词当提示词本身，库存素材拿它去搜索。只笼统说一句"关键词会
    覆盖逐句配画"，用户还是不知道自己这一次会得到什么。
    """
    if str(video_source or "") in material.GENERATED_IMAGE_SOURCES:
        return "Keywords Override Alignment Drawn"
    return "Keywords Override Alignment Searched"
