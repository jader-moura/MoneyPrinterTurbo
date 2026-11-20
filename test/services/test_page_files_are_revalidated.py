"""页面更新之后，普通的刷新就该拿到新的脚本。

没有 Cache-Control 的时候浏览器自己估"还新鲜"多久，于是更新之后是新的 html
配旧的 js：新按钮在，点了没反应。页面自己的文件每次都要核对一遍。

After the editor is updated, a normal reload must not run a stale script.
"""

from fastapi.testclient import TestClient

from app import asgi

client = TestClient(asgi.app)


def test_the_editors_page_script_and_styles_are_checked_on_every_load():
    for path in ("/editor/", "/editor/editor.js", "/editor/editor.css"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers.get("cache-control") == "no-cache", path


def test_an_unchanged_file_is_not_downloaded_again():
    first = client.get("/editor/editor.js")
    again = client.get("/editor/editor.js", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304
    assert again.headers.get("cache-control") == "no-cache"
