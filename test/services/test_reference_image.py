# -*- coding: utf-8 -*-
"""照一张真实照片画这一段。

用户的原话："the idea is that we could have a button to regenerate image but
getting a similar image from google, the app will do everything automatically,
search the image and generate it following the style selected"。

这里钉的是"自动"那三步里的后两步：搜什么词（``llm.image_search_query``）、
搜回来的东西能不能用（``reference_image``），以及那张照片怎么一路传到出图
请求里。一次网络请求都不会真的发出去。
"""

import base64
import io
import json

import pytest
from PIL import Image

from app.services import reference_image


def a_photo(width=1200, height=800, colour=(90, 110, 140), fmt="JPEG"):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format=fmt)
    return buffer.getvalue()


class FakeResponse:
    """requests 的响应，只有这个模块真正用到的那几项。"""

    def __init__(self, *, status=200, payload=None, body=b"", headers=None):
        self.status_code = status
        self._payload = payload
        self._body = body
        self.headers = headers or {}
        self.closed = False

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def iter_content(self, chunk_size=1):
        for at in range(0, len(self._body), chunk_size):
            yield self._body[at : at + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True
        return False


class FakeRequests:
    """按 URL 回答：搜索一条，下载一条。记下每次调用的参数。"""

    def __init__(self, search=None, downloads=None):
        self.search = search
        self.downloads = downloads or {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.startswith(
            (
                reference_image.GOOGLE_CSE_ENDPOINT,
                reference_image.BRAVE_ENDPOINT,
                reference_image.OPENVERSE_ENDPOINT,
            )
        ):
            if isinstance(self.search, Exception):
                raise self.search
            return self.search
        answer = self.downloads.get(url)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            return FakeResponse(status=404)
        return answer


def cse_hit(url, *, page="https://example.org/a-page", width=1200, height=800):
    return {"link": url, "image": {"contextLink": page, "width": width, "height": height}}


def found(*items):
    return FakeResponse(payload={"items": list(items)})


def photo_at(url, **kwargs):
    return {
        url: FakeResponse(
            body=a_photo(**kwargs), headers={"Content-Type": "image/jpeg"}
        )
    }


def brave_hit(url, *, page="https://example.org/a-page"):
    """Brave 的一条结果：图在 ``properties.url``，页面在 ``url``。"""
    return {"type": "image_result", "properties": {"url": url}, "url": page}


def brave_found(*items):
    return FakeResponse(payload={"type": "images", "results": list(items)})


CONFIG = {
    "reference_image_provider": "google_cse",
    "google_search_api_key": "test-key",
    "google_search_engine_id": "test-cx",
}

BRAVE_CONFIG = {
    "reference_image_provider": "brave",
    "brave_search_api_key": "brave-test-key",
}


def openverse_hit(
    url,
    *,
    page="https://flickr.example/photos/1",
    width=1600,
    height=1200,
    licence="by",
    version="2.0",
    creator="A Photographer",
):
    return {
        "url": url,
        "foreign_landing_url": page,
        "width": width,
        "height": height,
        "license": licence,
        "license_version": version,
        "creator": creator,
    }


def openverse_found(*items):
    return FakeResponse(payload={"result_count": len(items), "results": list(items)})


# 这就是重点：一把 key 都没有。
OPENVERSE_CONFIG = {"reference_image_provider": "openverse"}


@pytest.fixture
def web(monkeypatch):
    """requests 换成假的，并给一份够用的配置。"""

    def install(fake):
        monkeypatch.setattr(reference_image, "requests", fake)
        return fake

    return install


class TestItNeedsToBeConfigured:
    def test_it_names_both_missing_settings(self):
        with pytest.raises(reference_image.ReferenceImageError) as caught:
            reference_image.search("comet airliner", app_config={})
        message = str(caught.value)
        assert "google_search_api_key" in message
        assert "google_search_engine_id" in message
        # 去哪儿拿这两样东西也要说，不然用户只知道缺什么。
        assert "programmablesearchengine.google.com" in message

    def test_it_names_the_one_that_is_missing(self):
        with pytest.raises(reference_image.ReferenceImageError) as caught:
            reference_image.search(
                "comet", app_config={"google_search_api_key": "k"}
            )
        assert "google_search_engine_id" in str(caught.value)
        assert "google_search_api_key" not in str(caught.value)

    def test_an_unknown_provider_is_not_quietly_replaced(self):
        """换一个供应商等于换一批结果，用户按按钮时以为搜的是 Google。"""
        with pytest.raises(reference_image.ReferenceImageError) as caught:
            reference_image.search("comet", app_config={**CONFIG, "reference_image_provider": "bing"})
        assert "bing" in str(caught.value)
        assert "google_cse" in str(caught.value)
        # 认识哪几个要全说出来，不然用户不知道还有别的路。
        assert "brave" in str(caught.value)
        assert "openverse" in str(caught.value)

    def test_the_google_one_mentions_the_other_way_to_search(self):
        """Google 那套要先建搜索引擎，不是每个人都愿意。"""
        with pytest.raises(reference_image.ReferenceImageError) as caught:
            reference_image.search("comet", app_config={})
        assert "brave" in str(caught.value)

    def test_brave_names_the_setting_it_needs(self):
        with pytest.raises(reference_image.ReferenceImageError) as caught:
            reference_image.search(
                "comet", app_config={"reference_image_provider": "brave"}
            )
        message = str(caught.value)
        assert "brave_search_api_key" in message
        assert "api-dashboard.search.brave.com" in message
        # Brave 不用建引擎，这就是选它的理由，所以要说。
        assert "google_search_engine_id" not in message

    def test_an_empty_query_is_refused_before_anything_is_sent(self, web):
        fake = web(FakeRequests())
        with pytest.raises(reference_image.ReferenceImageError):
            reference_image.search("   ", app_config=CONFIG)
        assert fake.calls == []


class TestFindingOne:
    def test_it_comes_back_with_the_photo_and_where_it_came_from(self, web):
        url = "https://pics.example.org/comet.jpg"
        web(FakeRequests(search=found(cse_hit(url)), downloads=photo_at(url)))
        reference = reference_image.search("comet 1952", app_config=CONFIG)
        assert reference.provider == "google_cse"
        assert reference.query == "comet 1952"
        assert reference.source_url == url
        assert reference.page_url == "https://example.org/a-page"
        assert (reference.width, reference.height) == (1200, 800)
        assert reference.b64 == base64.b64encode(reference.data).decode("ascii")

    def test_the_photo_is_re_encoded_as_png(self, web):
        """重新编码会丢掉 EXIF（里头可能有拍摄地点）和多余的帧。"""
        url = "https://pics.example.org/comet.jpg"
        web(FakeRequests(search=found(cse_hit(url)), downloads=photo_at(url)))
        reference = reference_image.search("comet", app_config=CONFIG)
        assert reference.data[:8] == b"\x89PNG\r\n\x1a\n"
        with Image.open(io.BytesIO(reference.data)) as drawn:
            assert drawn.mode == "RGB"

    def test_it_asks_google_for_photographs(self, web):
        url = "https://pics.example.org/comet.jpg"
        fake = web(FakeRequests(search=found(cse_hit(url)), downloads=photo_at(url)))
        reference_image.search("comet", app_config=CONFIG)
        params = fake.calls[0][1]["params"]
        assert params["searchType"] == "image"
        assert params["imgType"] == "photo"
        assert params["safe"] == "active"
        assert params["cx"] == "test-cx"

    def test_the_payload_for_the_ui_leaves_the_bytes_out(self, web):
        url = "https://pics.example.org/comet.jpg"
        web(FakeRequests(search=found(cse_hit(url)), downloads=photo_at(url)))
        payload = reference_image.search("comet", app_config=CONFIG).payload()
        assert "data" not in payload and "b64" not in payload
        assert payload["page_url"] == "https://example.org/a-page"
        assert payload["query"] == "comet"


class TestWhatItRefuses:
    def test_a_hit_that_will_not_download_is_skipped_for_the_next_one(self, web):
        """搜索头几条打不开是常事：防盗链、403、已经删了。"""
        dead = "https://dead.example.org/gone.jpg"
        good = "https://pics.example.org/comet.jpg"
        web(
            FakeRequests(
                search=found(cse_hit(dead), cse_hit(good)),
                downloads={dead: FakeResponse(status=403), **photo_at(good)},
            )
        )
        assert reference_image.search("comet", app_config=CONFIG).source_url == good

    def test_something_that_is_not_an_image_is_skipped(self, web):
        page = "https://pics.example.org/not-a-photo.html"
        good = "https://pics.example.org/comet.jpg"
        web(
            FakeRequests(
                search=found(cse_hit(page), cse_hit(good)),
                downloads={
                    page: FakeResponse(
                        body=b"<html>nope</html>",
                        headers={"Content-Type": "text/html"},
                    ),
                    **photo_at(good),
                },
            )
        )
        assert reference_image.search("comet", app_config=CONFIG).source_url == good

    def test_a_thumbnail_is_not_good_enough_to_draw_from(self, web):
        """底图糊了，画出来的也糊。"""
        tiny = "https://pics.example.org/thumb.jpg"
        web(FakeRequests(search=found(cse_hit(tiny)), downloads=photo_at(tiny, width=200, height=120)))
        with pytest.raises(reference_image.ReferenceImageError) as caught:
            reference_image.search("comet", app_config=CONFIG)
        assert "512" in str(caught.value)

    def test_a_huge_file_is_cut_off_rather_than_swallowed(self, web, monkeypatch):
        monkeypatch.setattr(reference_image, "MAX_BYTES", 1024)
        big = "https://pics.example.org/huge.jpg"
        web(FakeRequests(search=found(cse_hit(big)), downloads=photo_at(big)))
        with pytest.raises(reference_image.ReferenceImageError) as caught:
            reference_image.search("comet", app_config=CONFIG)
        assert "limit" in str(caught.value)

    def test_a_lying_content_length_does_not_get_to_fill_memory(self, web, monkeypatch):
        monkeypatch.setattr(reference_image, "MAX_BYTES", 1024)
        big = "https://pics.example.org/huge.jpg"
        web(
            FakeRequests(
                search=found(cse_hit(big)),
                downloads={
                    big: FakeResponse(
                        body=a_photo(),
                        headers={"Content-Type": "image/jpeg", "Content-Length": "10"},
                    )
                },
            )
        )
        with pytest.raises(reference_image.ReferenceImageError):
            reference_image.search("comet", app_config=CONFIG)

    def test_it_gives_up_after_a_few_tries_rather_than_all_of_them(self, web):
        hits = [cse_hit(f"https://dead.example.org/{at}.jpg") for at in range(20)]
        fake = web(
            FakeRequests(
                search=found(*hits),
                downloads={hit["link"]: FakeResponse(status=404) for hit in hits},
            )
        )
        with pytest.raises(reference_image.ReferenceImageError) as caught:
            reference_image.search("comet", app_config=CONFIG)
        assert "none could be used" in str(caught.value)
        # 一次搜索加至多 MAX_ATTEMPTS 次下载。
        assert len(fake.calls) == 1 + reference_image.MAX_ATTEMPTS

    def test_no_results_says_what_was_searched_for(self, web):
        web(FakeRequests(search=FakeResponse(payload={})))
        with pytest.raises(reference_image.ReferenceImageError) as caught:
            reference_image.search("de havilland comet", app_config=CONFIG)
        assert "de havilland comet" in str(caught.value)

    def test_a_quota_error_from_google_is_passed_on_in_words(self, web):
        web(
            FakeRequests(
                search=FakeResponse(
                    status=429,
                    payload={
                        "error": {
                            "message": "Quota exceeded for quota metric 'Queries'",
                            "errors": [{"reason": "rateLimitExceeded"}],
                        }
                    },
                )
            )
        )
        with pytest.raises(reference_image.ReferenceImageError) as caught:
            reference_image.search("comet", app_config=CONFIG)
        assert "Quota exceeded" in str(caught.value)
        assert "rateLimitExceeded" in str(caught.value)

    def test_the_api_key_never_reaches_the_error_message(self, web):
        """错误里常带着整个请求 URL，而 URL 里有那把 key。"""
        web(FakeRequests(search=OSError("failed: ?key=test-key&cx=test-cx")))
        with pytest.raises(reference_image.ReferenceImageError) as caught:
            reference_image.search("comet", app_config=CONFIG)
        assert "test-key" not in str(caught.value)
        assert "OSError" in str(caught.value)


class TestBraveFindsThemToo:
    """同一个按钮，换一家搜索引擎。下载和检查那几道还是同一套。"""

    def test_it_comes_back_with_the_photo_and_where_it_came_from(self, web):
        url = "https://pics.example.org/comet.jpg"
        web(FakeRequests(search=brave_found(brave_hit(url)), downloads=photo_at(url)))
        reference = reference_image.search("comet 1952", app_config=BRAVE_CONFIG)
        assert reference.provider == "brave"
        assert reference.source_url == url
        assert reference.page_url == "https://example.org/a-page"
        assert reference.data[:8] == b"\x89PNG\r\n\x1a\n"

    def test_the_key_travels_in_the_header_not_the_url(self, web):
        """URL 会出现在日志和异常里，请求头不会。"""
        url = "https://pics.example.org/comet.jpg"
        fake = web(
            FakeRequests(search=brave_found(brave_hit(url)), downloads=photo_at(url))
        )
        reference_image.search("comet", app_config=BRAVE_CONFIG)
        endpoint, kwargs = fake.calls[0]
        assert endpoint == reference_image.BRAVE_ENDPOINT
        assert kwargs["headers"]["X-Subscription-Token"] == "brave-test-key"
        assert "brave-test-key" not in json.dumps(kwargs["params"])

    def test_safe_search_is_on_unless_it_is_turned_off(self, web):
        url = "https://pics.example.org/comet.jpg"
        fake = web(
            FakeRequests(search=brave_found(brave_hit(url)), downloads=photo_at(url))
        )
        reference_image.search("comet", app_config=BRAVE_CONFIG)
        assert fake.calls[0][1]["params"]["safesearch"] == "strict"

        fake = web(
            FakeRequests(search=brave_found(brave_hit(url)), downloads=photo_at(url))
        )
        reference_image.search(
            "comet", app_config={**BRAVE_CONFIG, "reference_image_safe": "off"}
        )
        assert fake.calls[0][1]["params"]["safesearch"] == "off"

    def test_a_hit_that_will_not_download_is_skipped_for_the_next_one(self, web):
        dead = "https://dead.example.org/gone.jpg"
        good = "https://pics.example.org/comet.jpg"
        web(
            FakeRequests(
                search=brave_found(brave_hit(dead), brave_hit(good)),
                downloads={dead: FakeResponse(status=403), **photo_at(good)},
            )
        )
        found_one = reference_image.search("comet", app_config=BRAVE_CONFIG)
        assert found_one.source_url == good

    def test_a_thumbnail_is_not_good_enough_here_either(self, web):
        """两家供应商必须共用同一套检查，不然换一家等于换一套。"""
        tiny = "https://pics.example.org/thumb.jpg"
        web(
            FakeRequests(
                search=brave_found(brave_hit(tiny)),
                downloads=photo_at(tiny, width=200, height=120),
            )
        )
        with pytest.raises(reference_image.ReferenceImageError) as caught:
            reference_image.search("comet", app_config=BRAVE_CONFIG)
        assert "512" in str(caught.value)

    def test_it_gives_up_after_a_few_tries_rather_than_all_of_them(self, web):
        hits = [brave_hit(f"https://dead.example.org/{at}.jpg") for at in range(20)]
        fake = web(
            FakeRequests(
                search=brave_found(*hits),
                downloads={
                    hit["properties"]["url"]: FakeResponse(status=404) for hit in hits
                },
            )
        )
        with pytest.raises(reference_image.ReferenceImageError) as caught:
            reference_image.search("comet", app_config=BRAVE_CONFIG)
        assert "none could be used" in str(caught.value)
        assert len(fake.calls) == 1 + reference_image.MAX_ATTEMPTS

    def test_no_results_says_what_was_searched_for(self, web):
        web(FakeRequests(search=FakeResponse(payload={"type": "images"})))
        with pytest.raises(reference_image.ReferenceImageError) as caught:
            reference_image.search("de havilland comet", app_config=BRAVE_CONFIG)
        message = str(caught.value)
        assert "de havilland comet" in message
        # 点名是谁没找到：用户配了两家里的哪一家，错误要自己说清楚。
        assert "brave" in message

    def test_a_refusal_from_brave_is_passed_on_in_words(self, web):
        web(
            FakeRequests(
                search=FakeResponse(
                    status=429,
                    payload={
                        "type": "ErrorResponse",
                        "error": {
                            "code": "RATE_LIMITED",
                            "detail": "Request rate limit exceeded",
                        },
                    },
                )
            )
        )
        with pytest.raises(reference_image.ReferenceImageError) as caught:
            reference_image.search("comet", app_config=BRAVE_CONFIG)
        assert "429" in str(caught.value)
        assert "Request rate limit exceeded" in str(caught.value)
        assert "RATE_LIMITED" in str(caught.value)

    def test_the_api_key_never_reaches_the_error_message(self, web):
        web(FakeRequests(search=OSError("failed talking to brave-test-key")))
        with pytest.raises(reference_image.ReferenceImageError) as caught:
            reference_image.search("comet", app_config=BRAVE_CONFIG)
        assert "brave-test-key" not in str(caught.value)
        assert "OSError" in str(caught.value)


class TestOpenverseNeedsNothing:
    """不要 key、不要账号、不要信用卡：配不起别的那两家时还剩这一条路。"""

    def test_it_searches_with_no_configuration_at_all(self, web):
        url = "https://live.static.example/comet_b.jpg"
        fake = web(
            FakeRequests(
                search=openverse_found(openverse_hit(url)), downloads=photo_at(url)
            )
        )
        reference = reference_image.search("comet 1952", app_config=OPENVERSE_CONFIG)
        assert reference.provider == "openverse"
        assert reference.source_url == url
        assert reference.page_url == "https://flickr.example/photos/1"
        # 没有 key，就没有任何一处会漏 key 的地方。
        assert "Authorization" not in fake.calls[0][1]["headers"]

    def test_the_licence_and_the_photographer_are_recorded(self, web):
        """这三家里只有这一家答得上"我能不能发出去"。"""
        url = "https://live.static.example/comet_b.jpg"
        web(
            FakeRequests(
                search=openverse_found(openverse_hit(url)), downloads=photo_at(url)
            )
        )
        reference = reference_image.search("comet", app_config=OPENVERSE_CONFIG)
        assert reference.licence == "CC BY 2.0"
        assert reference.creator == "A Photographer"
        payload = reference.payload()
        assert payload["license"] == "CC BY 2.0"
        assert payload["creator"] == "A Photographer"

    def test_public_domain_reads_as_public_domain(self, web):
        url = "https://live.static.example/comet_b.jpg"
        web(
            FakeRequests(
                search=openverse_found(
                    openverse_hit(url, licence="cc0", version="1.0", creator="")
                ),
                downloads=photo_at(url),
            )
        )
        reference = reference_image.search("comet", app_config=OPENVERSE_CONFIG)
        assert reference.licence == "CC0 1.0"

    def test_it_asks_for_photographs_it_may_redraw_and_publish(self, web):
        url = "https://live.static.example/comet_b.jpg"
        fake = web(
            FakeRequests(
                search=openverse_found(openverse_hit(url)), downloads=photo_at(url)
            )
        )
        reference_image.search("comet", app_config=OPENVERSE_CONFIG)
        params = fake.calls[0][1]["params"]
        assert params["category"] == "photograph"
        assert params["license_type"] == "commercial,modification"
        # mature=true 是"把成人内容也放进来"，所以 SafeSearch 开着就是 false。
        assert params["mature"] == "false"

    def test_the_licence_filter_can_be_turned_off(self, web):
        url = "https://live.static.example/comet_b.jpg"
        fake = web(
            FakeRequests(
                search=openverse_found(openverse_hit(url)), downloads=photo_at(url)
            )
        )
        reference_image.search(
            "comet", app_config={**OPENVERSE_CONFIG, "openverse_license_type": ""}
        )
        assert "license_type" not in fake.calls[0][1]["params"]

    def test_a_token_is_sent_when_there_is_one(self, web):
        url = "https://live.static.example/comet_b.jpg"
        fake = web(
            FakeRequests(
                search=openverse_found(openverse_hit(url)), downloads=photo_at(url)
            )
        )
        reference_image.search(
            "comet", app_config={**OPENVERSE_CONFIG, "openverse_api_token": "a-token"}
        )
        assert fake.calls[0][1]["headers"]["Authorization"] == "Bearer a-token"

    def test_a_picture_it_already_says_is_too_small_is_never_downloaded(self, web):
        """尺寸搜索结果里就有，不用先下回来再量。"""
        tiny = "https://live.static.example/thumb.jpg"
        good = "https://live.static.example/comet_b.jpg"
        fake = web(
            FakeRequests(
                search=openverse_found(
                    openverse_hit(tiny, width=320, height=240),
                    openverse_hit(good),
                ),
                downloads=photo_at(good),
            )
        )
        assert (
            reference_image.search("comet", app_config=OPENVERSE_CONFIG).source_url
            == good
        )
        assert [call[0] for call in fake.calls[1:]] == [good]

    def test_nothing_found_explains_that_the_catalogue_is_small(self, web):
        """这是它真正的代价，错误里要说出来，不然像是坏了。"""
        web(FakeRequests(search=openverse_found()))
        with pytest.raises(reference_image.ReferenceImageError) as caught:
            reference_image.search("de havilland comet", app_config=OPENVERSE_CONFIG)
        message = str(caught.value)
        assert "de havilland comet" in message
        assert "licensed" in message

    def test_a_gateway_hiccup_is_tried_once_more(self, monkeypatch, web):
        """504 是它自己的网关在打嗝，不该把用户那一下点击浪费掉。"""
        monkeypatch.setattr(reference_image.time, "sleep", lambda _: None)
        url = "https://live.static.example/comet_b.jpg"

        class FlakyOnce(FakeRequests):
            def get(self, address, **kwargs):
                if address.startswith(reference_image.OPENVERSE_ENDPOINT) and not self.calls:
                    self.calls.append((address, kwargs))
                    return FakeResponse(status=504, payload={})
                return super().get(address, **kwargs)

        web(
            FlakyOnce(
                search=openverse_found(openverse_hit(url)), downloads=photo_at(url)
            )
        )
        assert (
            reference_image.search("comet", app_config=OPENVERSE_CONFIG).source_url
            == url
        )

    def test_it_does_not_keep_trying_forever(self, monkeypatch, web):
        monkeypatch.setattr(reference_image.time, "sleep", lambda _: None)
        fake = web(FakeRequests(search=FakeResponse(status=504, payload={})))
        with pytest.raises(reference_image.ReferenceImageError) as caught:
            reference_image.search("comet", app_config=OPENVERSE_CONFIG)
        assert len(fake.calls) == reference_image.OPENVERSE_TRIES
        assert "504" in str(caught.value)

    def test_being_rate_limited_says_what_to_do_about_it(self, web):
        web(FakeRequests(search=FakeResponse(status=429, payload={})))
        with pytest.raises(reference_image.ReferenceImageError) as caught:
            reference_image.search("comet", app_config=OPENVERSE_CONFIG)
        assert "openverse_api_token" in str(caught.value)

    def test_the_same_checks_apply_as_everywhere_else(self, web):
        """换搜索引擎不能换掉下载那套检查。"""
        lying = "https://live.static.example/not-a-photo.html"
        web(
            FakeRequests(
                search=openverse_found(openverse_hit(lying)),
                downloads={
                    lying: FakeResponse(
                        body=b"<html>nope</html>",
                        headers={"Content-Type": "text/html"},
                    )
                },
            )
        )
        with pytest.raises(reference_image.ReferenceImageError) as caught:
            reference_image.search("comet", app_config=OPENVERSE_CONFIG)
        assert "none could be used" in str(caught.value)


class TestHowFarItMayMove:
    def test_the_default_keeps_the_composition(self):
        assert reference_image.strength({}) == 0.75

    @pytest.mark.parametrize("value", [0, 0.0, -1, 1.5, "lots", None])
    def test_an_impossible_setting_falls_back_to_the_default(self, value):
        assert reference_image.strength({"reference_image_strength": value}) == 0.75

    def test_a_sensible_setting_is_used(self):
        assert reference_image.strength({"reference_image_strength": 0.4}) == 0.4


class TestTheSearchQueryIsWrittenForSearching:
    """提示词带着画风，拿它去搜图搜回来的是别人画的漫画。"""

    def _ask(self, monkeypatch, answer):
        from app.services import llm

        monkeypatch.setattr(llm, "_request_terms", lambda prompt, config: answer)
        return llm

    def test_it_returns_the_short_phrase(self, monkeypatch):
        llm = self._ask(monkeypatch, ["de Havilland Comet 1952"])
        assert llm.image_search_query("jet airliners", "Britain launched it") == (
            "de Havilland Comet 1952"
        )

    def test_a_whole_sentence_is_cut_down(self, monkeypatch):
        llm = self._ask(
            monkeypatch,
            ["a de Havilland Comet airliner standing on a wet runway at dawn in 1952"],
        )
        query = llm.image_search_query("jets", "a line")
        assert len(query.split()) == llm.MAX_SEARCH_QUERY_WORDS

    def test_nothing_to_search_from_is_refused(self, monkeypatch):
        from app.services import llm

        with pytest.raises(llm.TermGenerationError):
            llm.image_search_query("", "", "")

    def test_an_empty_answer_is_refused_rather_than_searched_for(self, monkeypatch):
        """空词去搜等于随机挑一张图当底子，比不用底图更坏。"""
        llm = self._ask(monkeypatch, ["   "])
        with pytest.raises(llm.TermGenerationError):
            llm.image_search_query("jets", "a line")

    def test_the_prompt_tells_the_model_to_leave_the_style_out(self, monkeypatch):
        from app.services import llm

        seen = {}

        def capture(prompt, config=None):
            seen["prompt"] = prompt
            return ["comet 1952"]

        monkeypatch.setattr(llm, "_request_terms", capture)
        llm.image_search_query("jets", "a line", "comic book art, bold ink")
        assert "REAL PHOTOGRAPH" in seen["prompt"]
        assert "No style, medium or camera words" in seen["prompt"]
        # 这一段的提示词要给模型看见，它才知道这张画的是什么。
        assert "comic book art, bold ink" in seen["prompt"]


class TestThePhotoReachesTheImageRequest:
    def test_the_payload_carries_the_picture_and_the_strength(self, monkeypatch):
        from app.services import material

        sent = {}

        def fake_request(endpoint, payload):
            sent.update(payload)
            return None, "stopped before the network"

        monkeypatch.setattr(material, "_request_openai_image", fake_request)
        monkeypatch.setattr(
            material, "_openai_image_endpoint", lambda: ("http://local/v1/images/generations", "zimage-turbo")
        )
        material.generate_images_openai(
            search_term="a comet airliner",
            minimum_duration=4,
            reference_image="QUJD",
            reference_strength=0.6,
        )
        assert sent["image"] == "QUJD"
        assert sent["strength"] == 0.6

    def test_without_a_photo_not_one_extra_field_is_sent(self, monkeypatch):
        """不支持底图的服务端看到的东西必须和以前一模一样。"""
        from app.services import material

        sent = {}

        def fake_request(endpoint, payload):
            sent.update(payload)
            return None, "stopped before the network"

        monkeypatch.setattr(material, "_request_openai_image", fake_request)
        monkeypatch.setattr(
            material, "_openai_image_endpoint", lambda: ("http://local/v1/images/generations", "zimage-turbo")
        )
        material.generate_images_openai(search_term="a comet airliner", minimum_duration=4)
        assert "image" not in sent and "strength" not in sent

    def test_a_stock_source_refuses_the_photo_instead_of_ignoring_it(self):
        from app.services import material

        with pytest.raises(ValueError, match="reference photo"):
            material.download_videos(
                task_id="t",
                search_terms=["comet"],
                source="pexels",
                reference_image="QUJD",
            )


class TestWhatIsRecordedOnTheScene:
    def test_the_query_and_the_page_survive_a_save(self):
        """用户过几天要能回答"这张图是哪来的"。"""
        from app.services import scene_manifest

        scene = scene_manifest._normalise_scene(
            {
                "material": "a.mp4",
                "reference": {
                    "query": "comet 1952",
                    "provider": "google_cse",
                    "source_url": "https://pics.example.org/c.jpg",
                    "page_url": "https://example.org/page",
                    "width": 1200,
                    "height": 800,
                },
            },
            0,
        )
        assert scene["reference"]["query"] == "comet 1952"
        assert scene["reference"]["page_url"] == "https://example.org/page"
        assert scene["reference"]["width"] == 1200

    def test_the_licence_survives_a_save_too(self):
        """要署名的话，几天后还得找得到署谁。"""
        from app.services import scene_manifest

        scene = scene_manifest._normalise_scene(
            {
                "material": "a.mp4",
                "reference": {
                    "query": "comet 1952",
                    "provider": "openverse",
                    "source_url": "https://pics.example.org/c.jpg",
                    "page_url": "https://example.org/page",
                    "license": "CC BY 2.0",
                    "creator": "A Photographer",
                },
            },
            0,
        )
        assert scene["reference"]["license"] == "CC BY 2.0"
        assert scene["reference"]["creator"] == "A Photographer"

    def test_a_source_that_cannot_say_grows_no_empty_keys(self):
        """Google 和 Brave 说不出许可证，那就一个字都不要留。"""
        from app.services import scene_manifest

        scene = scene_manifest._normalise_scene(
            {
                "material": "a.mp4",
                "reference": {
                    "query": "comet",
                    "provider": "google_cse",
                    "source_url": "https://pics.example.org/c.jpg",
                    "page_url": "https://example.org/page",
                    "license": "",
                    "creator": "",
                },
            },
            0,
        )
        assert "license" not in scene["reference"]
        assert "creator" not in scene["reference"]

    def test_a_scene_drawn_the_ordinary_way_grows_no_new_key(self):
        from app.services import scene_manifest

        scene = scene_manifest._normalise_scene({"material": "a.mp4"}, 0)
        assert "reference" not in scene

    def test_rubbish_in_that_field_is_dropped_not_stored(self):
        from app.services import scene_manifest

        scene = scene_manifest._normalise_scene(
            {"material": "a.mp4", "reference": "not a record"}, 0
        )
        assert "reference" not in scene


class TestTheEditorAsksForIt:
    @staticmethod
    def _editor_source():
        from pathlib import Path

        root = Path(__file__).parent.parent.parent
        return (root / "resource" / "public" / "editor" / "editor.js").read_text(
            encoding="utf-8"
        )

    def test_there_is_a_button(self):
        source = self._editor_source()
        assert 'button("Redraw from a photo"' in source
        assert "body.from_photo = true;" in source

    def test_it_only_shows_up_for_a_source_that_can_do_it(self):
        source = self._editor_source()
        at = source.index('button("Redraw from a photo"')
        before = source[:at]
        assert before.rstrip().endswith("if (isGenerated(scene)) {") or (
            "if (isGenerated(scene)) {" in before[-600:]
        )

    def test_the_queue_row_says_what_it_searched_and_where_it_came_from(self):
        source = self._editor_source()
        assert "job.from_photo" in source
        assert "photo: ${job.search" in source
        assert "host(job.reference.page_url)" in source

    def test_the_api_route_takes_the_flag(self):
        from app.models.schema import SceneRegenerateRequest

        body = SceneRegenerateRequest()
        assert body.from_photo is False, "老调用方的行为必须一个字都不变"
        assert SceneRegenerateRequest(from_photo=True, search="comet").search == "comet"

    def test_the_queue_carries_it_to_the_worker(self):
        import inspect

        from app.services import scene_regen_queue

        source = inspect.getsource(scene_regen_queue._run)
        assert "if job.from_photo:" in source
        assert "find_reference_photo" in source
        # 找照片必须在出图之前：找不到就不该出一张没有底图的图。
        assert source.index("find_reference_photo") < source.index(
            "regenerate_material"
        )
