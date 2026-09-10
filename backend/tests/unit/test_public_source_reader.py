import gzip
import hashlib
import socket
import ssl
import threading
import time

import httpx
import pytest

from src.services.public_source_reader import PublicSourceReader


PUBLIC_IP = "93.184.216.34"
BODY = (
    "北京校园参观建议先查看学校公开预约说明，按预约时段从指定入口进入，不要将校门外参观写成进入校园。"
    "上午安排一所学校，午餐在附近已核实开放的餐厅解决，下午留出休息和交通时间。"
    "每天不要重复往返城市两端，遇到临时关闭应保留取消安排的余地，并且提前查看当天的通知。"
)


class Peer:
    def __init__(self, address=PUBLIC_IP):
        self.address = address

    def get_extra_info(self, name):
        return (self.address, 443) if name == "server_addr" else None


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        yield from self.chunks

    def close(self):
        self.closed = True


def response(body=None, *, status=200, headers=None, peer=PUBLIC_IP, chunks=None):
    data = body.encode("utf-8") if isinstance(body, str) else (body or b"")
    return httpx.Response(
        status,
        headers={"content-type": "text/html; charset=utf-8", **(headers or {})},
        stream=chunks or Chunks([data]),
        extensions={"network_stream": Peer(peer)} if peer else {},
    )


def make_reader(handler, *, resolver=None, clock=None):
    calls = []
    transports = []

    class Transport(httpx.MockTransport):
        closed = False

        def close(self):
            self.closed = True
            super().close()

    def transport_factory(**kwargs):
        calls.append(kwargs)
        transport = Transport(handler)
        transports.append(transport)
        return transport

    kwargs = {"resolver": resolver or (lambda host, port: {PUBLIC_IP}), "transport_factory": transport_factory}
    if clock is not None:
        kwargs["clock"] = clock
    return PublicSourceReader(**kwargs), calls, transports


@pytest.mark.parametrize("tag,kind", [("article", "article"), ("main", "main"), ("div", "visible_text")])
def test_reads_actual_visible_body_and_fingerprints_exact_returned_text(tag, kind):
    page = (
        '<html><head><title>北京参观攻略</title><meta name="description" content="搜索摘要不能当作正文"></head>'
        "<body><nav>导航摘要</nav><script>脚本摘要</script><noscript>开启脚本的摘要</noscript>"
        f'<{tag}><p>{BODY}</p><p hidden>隐藏的预约规则</p><span style="display:none">隐藏文本</span></{tag}>'
        "<footer>页脚摘要</footer></body></html>"
    )
    reader, calls, transports = make_reader(lambda request: response(page))
    result = reader.read("https://example.com/guide#section")
    assert result["status"] == "succeeded"
    assert result["reason"] is None
    assert result["canonicalUrl"] == "https://example.com/guide"
    assert result["title"] == "北京参观攻略"
    assert result["bodyText"] == BODY
    assert result["contentKind"] == kind
    assert result["contentFingerprint"] == hashlib.sha256(BODY.encode("utf-8")).hexdigest()
    assert result["fetchedAt"].endswith("Z")
    assert calls[0]["addresses"] == {PUBLIC_IP}
    assert all(item.closed for item in transports)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:password@example.com/guide",
        "http://localhost/guide",
        "http://127.0.0.1/guide",
        "http://[::1]/guide",
        "http://169.254.169.254/latest/meta-data",
        "https://example.com:8443/guide",
        "https://example.com/?token=secret",
        "https://example.com/?%74oken=secret",
        "https://example.com/?%2574oken=secret",
        "https://example.com/?Access-Token=secret",
        "https://example.com/?credentials[api_key]=secret",
        "https://example.com/?signature=secret",
        "https://example.com/?sessionid=secret",
        "https://example.com/?options[auth]=secret",
        "https://example.com/?options[key]=secret",
        "https://example.com\\@127.0.0.1/guide",
        "https://example.com/%0d%0aHost:localhost",
    ],
)
def test_unsafe_url_is_blocked_before_any_http_or_secret_echo(url):
    reader, calls, _ = make_reader(lambda request: pytest.fail("HTTP must not run"))
    result = reader.read(url)
    assert result["status"] == "blocked"
    assert result["canonicalUrl"] is None
    assert result["bodyText"] is None
    assert result["contentFingerprint"] is None
    assert "secret" not in str(result)
    assert calls == []


@pytest.mark.parametrize(
    "addresses", [set(), {"127.0.0.1"}, {"10.0.0.1", PUBLIC_IP}, {"100.64.0.1"}, {"::ffff:93.184.216.34"}]
)
def test_all_dns_answers_must_be_public_unambiguous_addresses(addresses):
    reader, calls, _ = make_reader(
        lambda request: pytest.fail("HTTP must not run"), resolver=lambda host, port: addresses
    )
    assert reader.read("https://example.com/guide")["status"] == "blocked"
    assert calls == []


def test_redirect_resolves_each_hop_before_sending_and_rejects_private_destination():
    requests = []

    def handler(request):
        requests.append(str(request.url))
        return response(status=302, headers={"location": "https://next.example/guide"})

    reader, _, transports = make_reader(
        handler, resolver=lambda host, port: {PUBLIC_IP if host == "example.com" else "10.0.0.1"}
    )
    result = reader.read("https://example.com/guide")
    assert result["status"] == "blocked"
    assert requests == ["https://example.com/guide"]
    assert all(item.closed for item in transports)


def test_public_redirect_keeps_only_final_body_and_does_not_forward_cookies():
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return response(status=302, headers={"location": "/final", "set-cookie": "auth=secret"})
        return response(f"<main><p>{BODY}</p></main>")

    reader, calls, _ = make_reader(handler)
    result = reader.read("https://example.com/guide")
    assert result["status"] == "succeeded"
    assert result["canonicalUrl"] == "https://example.com/final"
    assert len(calls) == 2
    assert all("cookie" not in item.headers and "authorization" not in item.headers for item in requests)


@pytest.mark.parametrize("peer", [None, "127.0.0.1", "93.184.216.35"])
def test_response_peer_is_required_even_for_injected_transport(peer):
    reader, _, transports = make_reader(lambda request: response(f"<main>{BODY}</main>", peer=peer))
    result = reader.read("https://example.com/guide")
    assert result["status"] == "blocked"
    assert result["reason"] in {"peer_address_unavailable", "peer_address_mismatch"}
    assert result["bodyText"] is None
    assert all(item.closed for item in transports)


@pytest.mark.parametrize(
    "page,reason",
    [
        (f'<title>{BODY}</title><meta name="description" content="{BODY}"><div id="app"></div>', "body_unavailable"),
        (f"<body><nav>{BODY}</nav><footer>{BODY}</footer><script>{BODY}</script></body>", "body_unavailable"),
        ("<title>安全验证</title><main>请完成验证码后继续</main>", "access_gate"),
        (f"<main><p>登录后查看全文</p><p>{BODY}</p></main>", "access_gate"),
        ("<title>Just a moment...</title><body>Checking your browser</body>", "access_gate"),
    ],
)
def test_summary_login_challenge_and_empty_shell_are_not_body_evidence(page, reason):
    reader, _, _ = make_reader(lambda request: response(page))
    result = reader.read("https://example.com/guide")
    assert result["status"] == "blocked"
    assert result["reason"] == reason
    assert result["bodyText"] is None
    assert result["contentFingerprint"] is None


@pytest.mark.parametrize("tag", ["div", "main", "article"])
def test_search_snippet_link_cards_are_not_read_article_bodies(tag):
    page = f'<{tag} class="search-results">' + "".join(f'<a href="/item{i}">{BODY}</a>' for i in range(3)) + f"</{tag}>"
    reader, _, _ = make_reader(lambda request: response(page))
    result = reader.read("https://example.com/guide")
    assert result["status"] == "blocked"
    assert result["reason"] == "body_unavailable"
    assert result["bodyText"] is None
    assert result["contentFingerprint"] is None


def test_div_only_article_with_inline_links_is_still_body():
    page = f'<div>{BODY}<a href="/booking">预约入口</a></div>'
    reader, _, _ = make_reader(lambda request: response(page))
    result = reader.read("https://example.com/guide")
    assert result["status"] == "succeeded"
    assert result["bodyText"] == BODY + "预约入口"


@pytest.mark.parametrize("wrapper", ["div", "main", "article"])
def test_non_link_snippets_in_result_page_semantics_are_not_article_bodies(wrapper):
    page = f'<{wrapper} class="search-results"><h2><a href="/full-guide">Guide result</a></h2><p>{BODY}</p></{wrapper}>'
    reader, _, _ = make_reader(lambda request: response(page))
    result = reader.read("https://example.com/search?q=guide")
    assert result["status"] == "blocked"
    assert result["reason"] == "body_unavailable"
    assert result["bodyText"] is None
    assert result["contentFingerprint"] is None


@pytest.mark.parametrize("card_tag", ["div", "article", "li"])
def test_repeated_linked_heading_and_non_link_snippet_cards_are_not_body(card_tag):
    page = (
        "<main>"
        + "".join(
            f'<{card_tag}><h2><a href="/guide{i}">Guide result {i}</a></h2><p>{BODY}</p></{card_tag}>' for i in range(3)
        )
        + "</main>"
    )
    reader, _, _ = make_reader(lambda request: response(page))
    result = reader.read("https://example.com/collections/campus")
    assert result["status"] == "blocked"
    assert result["reason"] == "body_unavailable"
    assert result["bodyText"] is None


def test_html_result_listing_semantics_without_search_url_are_not_body():
    page = f'<main aria-label="Search results"><article><h2><a href="/full-guide">Guide result</a></h2><p>{BODY}</p></article></main>'
    reader, _, _ = make_reader(lambda request: response(page))
    result = reader.read("https://example.com/lookup")
    assert result["status"] == "blocked"
    assert result["reason"] == "body_unavailable"


def test_real_article_can_have_linked_headings_references_and_multiple_sections():
    page = f'<article><h2><a href="/official-booking">官方预约说明</a></h2><p>{BODY}</p><h2><a href="https://authority.example/notice">参观注意事项</a></h2><p>{BODY}</p></article>'
    reader, _, _ = make_reader(lambda request: response(page))
    result = reader.read("https://example.com/campus-guide")
    assert result["status"] == "succeeded"
    assert result["contentKind"] == "article"
    assert result["bodyText"].count(BODY) == 2


def test_in_page_heading_anchors_do_not_turn_article_sections_into_result_cards():
    page = (
        "<article>"
        + "".join(f'<div><h2><a href="#section{i}">行程第{i}段</a></h2><p>{BODY}</p></div>' for i in range(3))
        + "</article>"
    )
    reader, _, _ = make_reader(lambda request: response(page))
    result = reader.read("https://example.com/campus-guide")
    assert result["status"] == "succeeded"
    assert result["bodyText"].count(BODY) == 3


@pytest.mark.parametrize(
    "headers,body,reason",
    [
        ({"content-type": "application/pdf"}, b"%PDF-1.7", "unsupported_content_type"),
        ({"content-encoding": "br"}, b"encoded", "unsupported_content_encoding"),
        ({"content-length": str(512 * 1024 + 1)}, b"", "response_too_large"),
        ({}, b"x" * (512 * 1024 + 1), "response_too_large"),
        ({"content-encoding": "gzip"}, gzip.compress(b"x" * (512 * 1024 + 1)), "decoded_body_too_large"),
        ({"content-encoding": "gzip"}, b"truncated", "invalid_content_encoding"),
        ({}, f"<main>{'正' * 24001}</main>".encode(), "body_text_too_large"),
    ],
    ids=["mime", "encoding", "length", "wire", "decompression", "broken-gzip", "visible-text"],
)
def test_mime_wire_decoded_and_visible_body_limits_fail_closed(headers, body, reason):
    reader, _, transports = make_reader(lambda request: response(body, headers=headers))
    result = reader.read("https://example.com/guide")
    assert result["status"] in {"blocked", "failed"}
    assert result["reason"] == reason
    assert result["bodyText"] is None
    assert all(item.closed for item in transports)


def test_bounded_gzip_is_decoded_as_body():
    reader, _, _ = make_reader(
        lambda request: response(gzip.compress(f"<main>{BODY}</main>".encode()), headers={"content-encoding": "gzip"})
    )
    assert reader.read("https://example.com/guide")["bodyText"] == BODY


def test_redirect_chain_has_one_shared_deadline():
    now = [0.0]
    requests = []

    def handler(request):
        requests.append(request)
        now[0] += 3
        return response(status=302, headers={"location": f"/next{len(requests)}"})

    reader, _, transports = make_reader(handler, clock=lambda: now[0])
    result = reader.read("https://example.com/guide")
    assert result["status"] == "failed"
    assert result["reason"] == "timeout"
    assert len(requests) == 3
    assert all(item.closed for item in transports)


def test_expired_caller_deadline_does_not_resolve_dns_or_start_http():
    reader, calls, _ = make_reader(
        lambda request: pytest.fail("HTTP must not run"),
        resolver=lambda host, port: pytest.fail("DNS must not run"),
        clock=lambda: 100.0,
    )
    result = reader.read("https://example.com/guide", deadline=99.0)
    assert result["status"] == "failed"
    assert result["reason"] == "timeout"
    assert result["bodyText"] is None
    assert calls == []


@pytest.mark.parametrize("caller_deadline,expected_requests", [(1.0, 1), (100.0, 3)])
def test_caller_deadline_can_shorten_but_never_expand_source_budget(caller_deadline, expected_requests):
    now = [0.0]
    requests = []

    def handler(request):
        requests.append(request)
        now[0] += 3
        return response(status=302, headers={"location": f"/next{len(requests)}"})

    reader, _, transports = make_reader(handler, clock=lambda: now[0])
    result = reader.read("https://example.com/guide", deadline=caller_deadline)
    assert result["status"] == "failed"
    assert result["reason"] == "timeout"
    assert len(requests) == expected_requests
    assert all(item.closed for item in transports)


def test_redirect_count_is_bounded():
    requests = []

    def handler(request):
        requests.append(request)
        return response(status=302, headers={"location": f"/next{len(requests)}"})

    reader, _, _ = make_reader(handler)
    assert reader.read("https://example.com/guide")["reason"] == "redirect_limit"
    assert len(requests) == PublicSourceReader.MAX_REDIRECTS + 1


def test_slow_body_cannot_renew_the_total_deadline_and_is_closed():
    now = [0.0]
    yielded = []

    def chunks():
        for _ in range(10):
            now[0] += 3
            yielded.append(True)
            yield b"<p>body</p>"

    stream = Chunks(chunks())
    reader, _, transports = make_reader(lambda request: response(chunks=stream), clock=lambda: now[0])
    result = reader.read("https://example.com/guide")
    assert result["status"] == "failed"
    assert result["reason"] == "timeout"
    assert len(yielded) == 3
    assert stream.closed
    assert all(item.closed for item in transports)


def test_transport_is_closed_when_setup_uses_remaining_deadline():
    now = [0.0]
    transport = httpx.MockTransport(lambda request: pytest.fail("HTTP must not run"))
    closed = []
    transport.close = lambda: closed.append(True)

    def factory(**kwargs):
        now[0] = 9
        return transport

    reader = PublicSourceReader(
        resolver=lambda host, port: {PUBLIC_IP}, transport_factory=factory, clock=lambda: now[0]
    )
    assert reader.read("https://example.com/guide")["reason"] == "timeout"
    assert closed


def test_cleanup_failure_does_not_leave_success_body_evidence():
    class BrokenClose(httpx.MockTransport):
        def close(self):
            raise OSError("socket close failed")

    reader = PublicSourceReader(
        resolver=lambda host, port: {PUBLIC_IP},
        transport_factory=lambda **kwargs: BrokenClose(lambda request: response(f"<main>{BODY}</main>")),
    )
    result = reader.read("https://example.com/guide")
    assert result["status"] == "failed"
    assert result["reason"] == "network_error"
    assert result["canonicalUrl"] is None
    assert result["bodyText"] is None
    assert result["contentFingerprint"] is None


def test_dns_timeout_does_not_start_late_http(monkeypatch):
    release = threading.Event()
    finished = threading.Event()

    def resolver(host, port):
        try:
            release.wait(1)
            return {PUBLIC_IP}
        finally:
            finished.set()

    reader, calls, _ = make_reader(lambda request: pytest.fail("HTTP must not run"), resolver=resolver)
    monkeypatch.setattr(reader, "TOTAL_SECONDS", 0.02)
    started = time.monotonic()
    try:
        result = reader.read("https://example.com/guide")
        assert time.monotonic() - started < 0.12
        assert result["reason"] == "timeout"
        assert calls == []
    finally:
        release.set()
        assert finished.wait(1)


def test_repeated_dns_timeouts_have_process_bounded_workers_and_no_http(monkeypatch):
    release = threading.Event()
    workers = []
    guard = threading.Lock()

    def resolver(host, port):
        with guard:
            workers.append(threading.current_thread())
        release.wait(2)
        return {PUBLIC_IP}

    reader, calls, _ = make_reader(lambda request: pytest.fail("HTTP must not run"), resolver=resolver)
    monkeypatch.setattr(reader, "TOTAL_SECONDS", 0.01)
    try:
        results = [reader.read("https://example.com/guide") for _ in range(12)]
        assert len(workers) <= 4
        assert sum(worker.is_alive() for worker in workers) <= 4
        assert any(result["reason"] == "timeout" for result in results)
        assert any(result["reason"] == "dns_capacity_exhausted" for result in results)
        assert all(result["reason"] in {"timeout", "dns_capacity_exhausted"} for result in results)
        assert calls == []
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=1)
            assert not worker.is_alive()


@pytest.mark.parametrize(
    "scheme,peer", [("http", PUBLIC_IP), ("https", PUBLIC_IP), ("http", "10.0.0.1"), ("https", "10.0.0.1")]
)
def test_real_transport_pins_before_any_http_write_and_preserves_host(monkeypatch, scheme, peer):
    made = []
    encoded = f"<main>{BODY}</main>".encode()
    raw_response = (
        b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: "
        + str(len(encoded)).encode()
        + b"\r\nConnection: close\r\n\r\n"
        + encoded
    )
    tls_hosts = []

    class FakeTLSContext:
        def set_alpn_protocols(self, protocols):
            assert protocols == ["http/1.1"]

        def wrap_socket(self, sock, server_hostname):
            assert peer == PUBLIC_IP
            tls_hosts.append(server_hostname)
            return sock

    class FakeSocket:
        def __init__(self, *args):
            self.connected = None
            self.writes = []
            self.closed = False
            self.incoming = raw_response
            made.append(self)

        def settimeout(self, value):
            assert 0 < value <= 8

        def setsockopt(self, *args):
            pass

        def connect(self, target):
            self.connected = target

        def getpeername(self):
            return (peer, 80 if scheme == "http" else 443)

        def send(self, data):
            assert self.connected[0] == PUBLIC_IP
            self.writes.append(bytes(data))
            return len(data)

        def recv(self, count):
            data, self.incoming = self.incoming[:count], self.incoming[count:]
            return data

        def close(self):
            self.closed = True

    monkeypatch.setattr(socket, "socket", FakeSocket)
    monkeypatch.setattr(ssl, "create_default_context", FakeTLSContext)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9999")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9999")
    reader = PublicSourceReader(resolver=lambda host, port: {PUBLIC_IP})
    result = reader.read(f"{scheme}://example.com/guide")
    assert len(made) == 1
    assert made[0].connected == (PUBLIC_IP, 80 if scheme == "http" else 443)
    assert made[0].closed
    if peer != PUBLIC_IP:
        assert result["status"] == "blocked"
        assert result["reason"] == "peer_address_mismatch"
        assert made[0].writes == []
        assert tls_hosts == []
    else:
        assert result["status"] == "succeeded"
        assert b"Host: example.com\r\n" in b"".join(made[0].writes)
        assert tls_hosts == (["example.com"] if scheme == "https" else [])
