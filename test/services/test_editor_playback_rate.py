"""编辑器的预览可以放快：1×、1.5×、2×。

审一条十几分钟的片子，按原速就是十几分钟。倍数只管预览——旁白是时间基准，
它快了播放头就快了，画面、配乐、环境声和重音都得跟着同一个倍数，不然就是
声音在两倍、配乐在原速。出片不受影响。

The editor's preview can play at 1x, 1.5x or 2x. These pin that every element
that makes sound or picture follows the same rate, and that the rate survives
a clip change (setting ``src`` resets ``playbackRate``).
"""

from pathlib import Path

PAGE = Path(__file__).resolve().parents[2] / "resource/public/editor"
JS = (PAGE / "editor.js").read_text(encoding="utf-8").replace("\r\n", "\n")
HTML = (PAGE / "index.html").read_text(encoding="utf-8").replace("\r\n", "\n")


def _function(name):
    return JS.split(f"\nfunction {name}(")[1].split("\n}\n")[0]


def test_the_button_sits_with_play_and_says_it_is_only_the_preview():
    transport = HTML.split('<div class="transport">')[1].split("</div>")[0]
    assert transport.index('id="play"') < transport.index('id="rate"')
    assert "The rendered video is not affected." in transport


def test_the_rates_are_one_one_and_a_half_and_two():
    assert "const RATES = [1, 1.5, 2];" in JS
    assert "RATES[(RATES.indexOf(state.rate) + 1) % RATES.length]" in _function("nextRate")


def test_everything_that_plays_follows_the_same_rate():
    paced = _function("pacedMedia")
    for element in ("ui.narration", "ui.preview", "ui.musicBed",
                    "directionSounds.beds", "directionSounds.hits"):
        assert element in paced
    body = _function("applyRate")
    assert "media.playbackRate = state.rate;" in body
    # 换片段（换 src）会把 playbackRate 重置成 defaultPlaybackRate。
    assert "media.defaultPlaybackRate = state.rate;" in body


def test_it_is_applied_when_playing_and_to_sounds_made_later():
    assert "applyRate();" in _function("play")
    assert "applyRate();" in _function("prepareDirectionSounds")


def test_the_clock_without_a_narration_runs_at_the_rate_too():
    assert "state.playhead += state.rate / 60;" in _function("tick")


def test_the_choice_is_remembered_and_a_bad_one_is_ignored():
    assert "localStorage.setItem(RATE_KEY, String(state.rate))" in _function("nextRate")
    assert "RATES.includes(saved) ? saved : 1" in _function("savedRate")
