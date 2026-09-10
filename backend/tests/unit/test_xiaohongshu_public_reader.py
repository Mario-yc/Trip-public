import hashlib
import json
import logging
import socket
import ssl

import pytest

from backend.tests.unit.test_public_source_reader import PUBLIC_IP, make_reader, response
from src.services.public_source_reader import PublicSourceReader
from src.services.xiaohongshu_public_reader import XiaohongshuPublicReader


NOTE = "6a68780a000000001d00f421"
OTHER = "6a68780a000000001d00f422"
URL = f"https://www.xiaohongshu.com/discovery/item/{NOTE}"
TOKEN = "PublicShareToken_ForOfflineTests"
DESC = "上午安排校园参观，需提前核实开放和预约要求。\n下午在附近休息，避免重复跨城往返。\n正文中的 undefined 与表情 ✌🏻 都应按原文保留。"


def page(*, note_id=NOTE, desc=DESC, title="出行攻略", images=None, extra_notes=None):
    note = {
        "noteId": note_id,
        "title": title,
        "desc": desc,
        "xsecToken": TOKEN,
        "imageList": images
        or [
            {
                "fileId": "image-1",
                "width": 1200,
                "height": 1600,
                "urlDefault": "http://sns-webpic-qc.xhscdn.com/public/image.webp",
            }
        ],
    }
    state = {"note": {"noteDetailMap": {note_id: {"note": note}, **(extra_notes or {})}}}
    return (
        "<html><title>正文页面</title><script>window.__INITIAL_STATE__="
        + json.dumps(state, ensure_ascii=False)
        + ";</script></html>"
    )


def read_fixture(html, url=URL):
    base, calls, transports = make_reader(lambda request: response(html))
    reader = XiaohongshuPublicReader(resolver=lambda host, port: {PUBLIC_IP}, transport_factory=base.transport_factory)
    return reader.read(url), calls, transports


def test_exact_note_body_images_and_public_identity_do_not_leak_token():
    result, _, transports = read_fixture(
        page(), URL + f"?xsec_token={TOKEN}&xsec_source=pc_share&share_from_user_hidden=true"
    )
    assert result["status"] == "succeeded"
    assert result["noteId"] == NOTE
    assert result["canonicalUrl"] == URL
    assert result["bodyText"] == "出行攻略\n\n" + DESC
    assert result["contentFingerprint"] == hashlib.sha256(result["bodyText"].encode()).hexdigest()
    assert result["images"][0]["width"] == 1200
    assert result["images"][0]["readStatus"] == "not_read"
    assert result["parserVersion"] == XiaohongshuPublicReader.PARSER_VERSION
    assert TOKEN not in json.dumps(result)
    assert all(transport.closed for transport in transports)


def test_cn_short_link_uses_strict_ssr_after_public_share_redirect():
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return response(status=302, headers={"location": URL + f"?xsec_token={TOKEN}&xsec_source=pc_share"})
        return response(page())

    base, calls, _ = make_reader(handler)
    reader = XiaohongshuPublicReader(resolver=lambda host, port: {PUBLIC_IP}, transport_factory=base.transport_factory)
    result = reader.read("https://xhslink.cn/o/abc123")
    assert result["status"] == "succeeded"
    assert len(calls) == 2
    assert "xsec_token" not in requests[1].url.params
    assert ("xsec_token=" + TOKEN).encode() in requests[1].extensions["public_source_request_target"]
    assert "cookie" not in requests[1].headers and "referer" not in requests[1].headers
    assert TOKEN not in json.dumps(result)


@pytest.mark.parametrize(
    "url",
    [
        URL.replace("https:", "http:") + f"?xsec_token={TOKEN}",
        URL.replace("www.", "evil.") + f"?xsec_token={TOKEN}",
        "https://xhslink.cn/o/abc?xsec_token=" + TOKEN,
        URL + f"?xsec_token={TOKEN}&xsec_token={TOKEN}",
        URL + "?xsec_token=",
        URL + "?xsec_token=bad%0D%0Avalue",
        URL + f"?xsec_token={TOKEN}&redirect=https://example.com",
        URL + f"?xsec_token={TOKEN}&access_token=private",
        URL + f"?%2578sec_token={TOKEN}",
    ],
)
def test_share_token_exception_has_exact_host_path_and_parameter_scope(url):
    result, calls, _ = read_fixture(page(), url)
    assert result["status"] == "blocked"
    assert result["bodyText"] is None
    assert calls == []
    assert TOKEN not in json.dumps(result)


def test_generic_reader_still_rejects_xhs_share_tokens():
    base, calls, _ = make_reader(lambda request: pytest.fail("generic reader must not fetch"))
    assert isinstance(base, PublicSourceReader)
    assert base.read(URL + f"?xsec_token={TOKEN}")["reason"] == "sensitive_query"
    assert calls == []


@pytest.mark.parametrize(
    "url",
    [
        "http://xhslink.cn/o/abc123",
        URL.replace("https:", "http:"),
        URL.replace("www.xiaohongshu.com", "www.xiaohongshu.com:80"),
    ],
)
def test_xhs_requires_https_before_dns_or_http_even_without_share_tokens(url):
    reader = XiaohongshuPublicReader(
        resolver=lambda host, port: pytest.fail("insecure URL must not resolve"),
        transport_factory=lambda **kwargs: pytest.fail("insecure URL must not connect"),
    )
    result = reader.read(url)
    assert result["status"] == "blocked"
    assert result["bodyText"] is None
    assert result["canonicalUrl"] is None


@pytest.mark.parametrize("field", ["title", "desc", "image"])
def test_share_token_reflected_in_note_content_fails_closed(field):
    changes = (
        {field: TOKEN}
        if field != "image"
        else {"images": [{"urlDefault": f"https://sns-webpic-qc.xhscdn.com/{TOKEN}.webp", "width": 12, "height": 16}]}
    )
    if field == "desc":
        changes[field] += DESC
    result, _, _ = read_fixture(page(**changes), URL + f"?xsec_token={TOKEN}")
    assert result["status"] == "blocked"
    assert result["reason"] == "xhs_sensitive_content"
    assert result["bodyText"] is None and result["contentFingerprint"] is None
    assert result["title"] is None and result["images"] == []
    assert TOKEN not in json.dumps(result)


def test_short_link_redirect_token_is_checked_when_note_omits_structural_token():
    redirected_token = "DifferentOpaqueShareValue123="
    html = page(desc=DESC + "\n" + redirected_token).replace(f'"xsecToken": "{TOKEN}"', '"xsecToken": ""')
    count = 0

    def handler(request):
        nonlocal count
        count += 1
        if count == 1:
            return response(status=302, headers={"location": URL + f"?xsec_token={redirected_token}"})
        return response(html)

    base, calls, _ = make_reader(handler)
    reader = XiaohongshuPublicReader(resolver=lambda host, port: {PUBLIC_IP}, transport_factory=base.transport_factory)
    result = reader.read("https://xhslink.cn/o/abc123")
    assert len(calls) == 2
    assert result["reason"] == "xhs_sensitive_content"
    assert result["bodyText"] is None and result["contentFingerprint"] is None
    assert redirected_token not in json.dumps(result)


def test_structural_share_token_reflection_rejected_by_offline_extractor():
    result = XiaohongshuPublicReader.extract_html(page(desc=DESC + TOKEN), canonical_url=URL)
    assert result["status"] == "blocked"
    assert result["reason"] == "xhs_sensitive_content"
    assert TOKEN not in json.dumps(result)


def test_percent_encoded_share_value_cannot_escape_content_check():
    token = "OpaquePublicShareValue123="
    result = XiaohongshuPublicReader.extract_html(
        page(desc=DESC + token.replace("=", "%253D")), canonical_url=URL + "?xsec_token=" + token
    )
    assert result["reason"] == "xhs_sensitive_content"
    assert result["bodyText"] is None and result["title"] is None
    assert "OpaquePublicShareValue" not in json.dumps(result)


def test_share_token_cannot_follow_a_redirect_to_another_note():
    base, calls, _ = make_reader(
        lambda request: response(status=302, headers={"location": URL.replace(NOTE, OTHER) + f"?xsec_token={TOKEN}"})
    )
    reader = XiaohongshuPublicReader(resolver=lambda host, port: {PUBLIC_IP}, transport_factory=base.transport_factory)
    result = reader.read(URL + f"?xsec_token={TOKEN}")
    assert result["status"] == "blocked"
    assert len(calls) == 1
    assert TOKEN not in json.dumps(result)


@pytest.mark.parametrize(
    "html,reason",
    [
        ('<title>攻略</title><meta name="description" content="' + DESC + '">', "xhs_state_missing"),
        (page(note_id=OTHER), "xhs_note_identity_mismatch"),
        (page().replace('"noteId": "' + NOTE + '"', '"noteId": "' + OTHER + '"'), "xhs_note_identity_mismatch"),
        (page() + page(), "xhs_state_ambiguous"),
        (page().replace(";</script>", ";dangerous();</script>"), "xhs_state_invalid"),
        ("<script>window.__INITIAL_STATE__=eval('not-json')</script>", "xhs_state_invalid"),
        ("<title>安全验证</title>" + page(), "access_restricted"),
    ],
)
def test_missing_ambiguous_wrong_note_or_executable_state_cannot_be_body(html, reason):
    result, _, _ = read_fixture(html)
    assert result["status"] in {"blocked", "failed"}
    assert result["reason"] == reason
    assert result["bodyText"] is None
    assert result["contentFingerprint"] is None


def test_bare_undefined_is_lexical_data_not_global_text_replacement():
    html = page().replace('"xsecToken": "' + TOKEN + '"', '"unused": undefined, "values": [undefined, "undefined"]')
    result, _, _ = read_fixture(html)
    assert result["status"] == "succeeded"
    assert "undefined" in result["bodyText"]


def test_related_note_is_not_selected_and_image_metadata_is_not_image_reading():
    html = page(extra_notes={OTHER: {"note": {"noteId": OTHER, "title": "推荐卡", "desc": "推荐卡不是正文"}}})
    result, _, _ = read_fixture(html)
    assert result["status"] == "succeeded"
    assert "推荐卡" not in result["bodyText"]
    assert all(image["readStatus"] == "not_read" for image in result["images"])


def test_pictures_without_note_text_are_truthfully_unread_material():
    result, _, _ = read_fixture(page(desc=""))
    assert result["status"] == "blocked"
    assert result["reason"] == "xhs_note_text_unavailable"
    assert result["bodyText"] is None
    assert result["images"] and result["images"][0]["readStatus"] == "not_read"


def test_real_transport_sends_validated_token_only_on_wire_not_httpx_logs(monkeypatch, caplog):
    wire = []
    created = []
    payload = page().encode("utf-8")
    raw = (
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nConnection: close\r\nContent-Length: "
        + str(len(payload)).encode()
        + b"\r\n\r\n"
        + payload
    )

    class Socket:
        def __init__(self, *args):
            self.incoming = raw
            self.closed = False
            created.append(self)

        def settimeout(self, timeout):
            assert 0 < timeout <= 8

        def setsockopt(self, *args):
            pass

        def connect(self, target):
            assert target == (PUBLIC_IP, 443)

        def getpeername(self):
            return PUBLIC_IP, 443

        def send(self, data):
            wire.append(bytes(data))
            return len(data)

        def recv(self, count):
            current, self.incoming = self.incoming[:count], self.incoming[count:]
            return current

        def close(self):
            self.closed = True

    class TLS:
        def set_alpn_protocols(self, protocols):
            pass

        def wrap_socket(self, sock, server_hostname):
            assert server_hostname == "www.xiaohongshu.com"
            return sock

    monkeypatch.setattr(socket, "socket", Socket)
    monkeypatch.setattr(ssl, "create_default_context", TLS)
    with caplog.at_level(logging.INFO, logger="httpx"):
        result = XiaohongshuPublicReader(resolver=lambda host, port: {PUBLIC_IP}).read(
            URL + f"?xsec_token={TOKEN}&xsec_source=pc_share"
        )
    assert result["status"] == "succeeded"
    assert ("xsec_token=" + TOKEN).encode() in b"".join(wire)
    assert TOKEN not in caplog.text and TOKEN not in json.dumps(result)
    assert len(created) == 1 and created[0].closed


@pytest.mark.parametrize(
    "token,allowed", [(TOKEN + "=", True), (TOKEN + "==", True), ("in=the=middle", False), (TOKEN + "===", False)]
)
def test_share_padding_is_bounded_and_only_at_end(token, allowed):
    result, _, _ = read_fixture(page(), URL + "?xsec_token=" + token)
    assert (result["status"] == "succeeded") is allowed
    assert token not in json.dumps(result)


def test_comment_or_string_fake_state_is_not_an_assignment():
    fake = (
        '<script>const example = "window.__INITIAL_STATE__={}";</script><script>// window.__INITIAL_STATE__={}</script>'
    )
    actual = page().replace("window.__INITIAL_STATE__=", "/* server data */\nwindow.__INITIAL_STATE__ = ")
    result, _, _ = read_fixture(fake + actual)
    assert result["status"] == "succeeded"


@pytest.mark.parametrize("change", ["deep", "duplicate", "nan", "unclosed"])
def test_state_depth_duplicate_keys_and_non_json_literals_fail_closed(change):
    html = page()
    if change == "deep":
        html = html.replace('"note":', '"extra":' + "[" * 65 + "0" + "]" * 65 + ',"note":', 1)
    elif change == "duplicate":
        html = html.replace('"note":', '"note":{},"note":', 1)
    elif change == "nan":
        html = html.replace('"xsecToken": "' + TOKEN + '"', '"unused": NaN')
    else:
        html = html.replace(";</script>", "</script>").replace('"noteId":', '"unterminated:')
    result, _, _ = read_fixture(html)
    assert result["status"] == "blocked"
    assert result["reason"] == "xhs_state_invalid"
    assert result["bodyText"] is None


def test_image_metadata_never_fetches_subresources_or_keeps_unsafe_urls():
    images = [
        {"width": 1200, "height": 1600, "urlDefault": value}
        for value in [
            "file:///secret",
            "data:image/png,private",
            "https://127.0.0.1/private",
            "https://user:secret@sns-webpic-qc.xhscdn.com/image.webp",
            "http://sns-webpic-qc.xhscdn.com/image.webp?xsec_token=" + TOKEN,
        ]
    ]
    result, calls, _ = read_fixture(page(images=images))
    assert result["status"] == "succeeded"
    assert len(calls) == 1
    assert result["images"] == [
        {"url": "http://sns-webpic-qc.xhscdn.com/image.webp", "width": 1200, "height": 1600, "readStatus": "not_read"}
    ]
    assert TOKEN not in json.dumps(result)
