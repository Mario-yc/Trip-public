"""Bounded, read-only retrieval of public source bodies; no search, browser or DB writes."""

from __future__ import annotations

import hashlib
import ipaddress
import queue
import re
import socket
import ssl
import threading
import time
import zlib
from contextlib import ExitStack
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Callable, Literal, Optional, TypedDict
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit, urlunsplit

import httpcore
import httpx


# DNS cannot be safely interrupted in CPython. Bound outstanding OS lookups
# process-wide; timed-out callers never enqueue another HTTP operation.
_DNS_CAPACITY = 4
_DNS_ADMISSION = threading.BoundedSemaphore(_DNS_CAPACITY)


class PublicSourceReadResult(TypedDict):
    status: Literal["succeeded", "blocked", "failed"]
    reason: Optional[str]
    canonicalUrl: Optional[str]
    title: Optional[str]
    bodyText: Optional[str]
    contentFingerprint: Optional[str]
    fetchedAt: str
    contentKind: Optional[Literal["article", "main", "visible_text"]]


class _ReadFailure(Exception):
    def __init__(self, status: str, reason: str):
        self.status = status
        self.reason = reason
        super().__init__(reason)


def _remaining(deadline: float, clock: Callable[[], float], cap: float = 8.0) -> float:
    value = min(cap, deadline - clock())
    if value <= 0:
        raise _ReadFailure("failed", "timeout")
    return value


def _public_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise _ReadFailure("blocked", "non_public_address") from None
    if not address.is_global or address.is_multicast or getattr(address, "ipv4_mapped", None) or "%" in value:
        raise _ReadFailure("blocked", "non_public_address")
    return str(address)


def _validate_peer(stream, addresses: set[str]) -> None:
    get_info = getattr(stream, "get_extra_info", None)
    peer = get_info("server_addr") if callable(get_info) else None
    raw = peer[0] if isinstance(peer, (tuple, list)) and peer else peer
    if raw is None:
        raise _ReadFailure("blocked", "peer_address_unavailable")
    try:
        address = _public_ip(str(raw))
    except _ReadFailure:
        raise _ReadFailure("blocked", "peer_address_mismatch") from None
    if address not in addresses:
        raise _ReadFailure("blocked", "peer_address_mismatch")


class _PinnedSocketStream(httpcore.NetworkStream):
    def __init__(self, sock, *, addresses, deadline, clock):
        self.sock = sock
        self.addresses = addresses
        self.deadline = deadline
        self.clock = clock

    def _timeout(self, requested, cap):
        return _remaining(self.deadline, self.clock, min(requested or cap, cap))

    def read(self, max_bytes: int, timeout=None) -> bytes:
        self.sock.settimeout(self._timeout(timeout, 4.0))
        return self.sock.recv(max_bytes)

    def write(self, buffer: bytes, timeout=None) -> None:
        # Check before the first HTTP byte, not merely after response headers.
        _validate_peer(self, self.addresses)
        while buffer:
            self.sock.settimeout(self._timeout(timeout, 3.0))
            sent = self.sock.send(buffer)
            if sent <= 0:
                raise OSError("socket_closed")
            buffer = buffer[sent:]

    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        _validate_peer(self, self.addresses)
        try:
            self.sock.settimeout(self._timeout(timeout, 3.0))
            self.sock = ssl_context.wrap_socket(self.sock, server_hostname=server_hostname)
            _validate_peer(self, self.addresses)
            return self
        except Exception:
            self.close()
            raise

    def get_extra_info(self, info):
        if info == "server_addr":
            return self.sock.getpeername()
        if info == "ssl_object":
            return getattr(self.sock, "_sslobj", None)
        return None

    def close(self):
        self.sock.close()


class _PinnedNetworkBackend(httpcore.NetworkBackend):
    def __init__(self, *, host, port, addresses, deadline, clock):
        self.host, self.port = host, port
        self.addresses = addresses
        self.deadline, self.clock = deadline, clock

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if host != self.host or port != self.port or local_address is not None:
            raise _ReadFailure("blocked", "connection_scope_mismatch")
        # Numeric socket.connect does not resolve the untrusted hostname again.
        address = sorted(self.addresses)[0]
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        stream = _PinnedSocketStream(sock, addresses=self.addresses, deadline=self.deadline, clock=self.clock)
        try:
            sock.settimeout(_remaining(self.deadline, self.clock, min(timeout or 3.0, 3.0)))
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.connect((address, port))
            _validate_peer(stream, self.addresses)
            return stream
        except Exception:
            stream.close()
            raise


class _ResponseStream(httpx.SyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    def __iter__(self):
        yield from self.stream

    def close(self):
        self.stream.close()


class _PinnedTransport(httpx.BaseTransport):
    def __init__(self, **kwargs):
        self.pool = httpcore.ConnectionPool(
            ssl_context=ssl.create_default_context(),
            network_backend=_PinnedNetworkBackend(**kwargs),
            max_connections=1,
            max_keepalive_connections=0,
            retries=0,
        )

    def handle_request(self, request):
        response = self.pool.handle_request(
            httpcore.Request(
                method=request.method,
                url=httpcore.URL(
                    scheme=request.url.raw_scheme,
                    host=request.url.raw_host,
                    port=request.url.port,
                    target=request.extensions.get("public_source_request_target", request.url.raw_path),
                ),
                headers=request.headers.raw,
                content=request.stream,
                extensions=request.extensions,
            )
        )
        return httpx.Response(
            response.status,
            headers=response.headers,
            stream=_ResponseStream(response.stream),
            extensions=response.extensions,
        )

    def close(self):
        self.pool.close()


def _clean_text(value: str) -> str:
    return "\n\n".join(line for line in (re.sub(r"\s+", " ", part).strip() for part in value.splitlines()) if line)


class _BodyParser(HTMLParser):
    HIDDEN = {
        "head",
        "script",
        "style",
        "noscript",
        "svg",
        "template",
        "nav",
        "footer",
        "header",
        "aside",
        "form",
        "button",
    }
    VOID = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
    BLOCK = {
        "article",
        "main",
        "section",
        "div",
        "p",
        "li",
        "ul",
        "ol",
        "h1",
        "h2",
        "h3",
        "h4",
        "blockquote",
        "br",
        "hr",
        "tr",
    }

    def __init__(self, deadline, clock):
        super().__init__(convert_charrefs=True)
        self.deadline, self.clock = deadline, clock
        self.stack = []
        self.regions = []
        self.visible = []
        self.non_link = []
        self.title = []
        self.nodes = 0
        self.node_ids = []
        self.cards = {}
        self.heading_links = set()
        self.result_listing = False

    def _append(self, text):
        self.visible.append(text)
        is_link = any(tag == "a" for tag, _, _ in self.stack)
        if not is_link:
            self.non_link.append(text)
        for node_id in self.node_ids:
            card = self.cards.get(node_id)
            if card is not None:
                if not is_link:
                    card["prose_size"] += len(text.strip())
                if any(node_id in self.heading_links for node_id in self.node_ids) and any(
                    tag in {"h1", "h2", "h3", "h4"} for tag, _, _ in self.stack
                ):
                    card["linked_heading"] = True
        for _, hidden, region in self.stack:
            if region is not None and not hidden:
                self.regions[region][1].append(text)
                if not is_link:
                    self.regions[region][2].append(text)

    def handle_starttag(self, tag, attrs):
        _remaining(self.deadline, self.clock)
        self.nodes += 1
        if self.nodes > 15000 or len(self.stack) >= 128:
            raise _ReadFailure("failed", "html_complexity_limit")
        attributes = dict(attrs)
        hidden = (
            bool(self.stack and self.stack[-1][1])
            or tag in self.HIDDEN
            or "hidden" in attributes
            or "inert" in attributes
            or (attributes.get("aria-hidden") or "").lower() == "true"
            or bool(re.search(r"(?:display\s*:\s*none|visibility\s*:\s*hidden)", attributes.get("style") or "", re.I))
        )
        if not hidden and tag in {"main", "section", "div", "ul", "ol"}:
            semantics = re.sub(
                r"[^a-z]",
                "",
                " ".join(attributes.get(key) or "" for key in ("id", "class", "aria-label", "data-testid")).lower(),
            )
            if any(
                marker in semantics
                for marker in ("searchresults", "searchresultlist", "searchlisting", "resultlist", "serpresults")
            ):
                self.result_listing = True
        if not hidden and tag in {"article", "li", "div"}:
            self.cards[self.nodes] = {
                "parent": self.node_ids[-1] if self.node_ids else None,
                "linked_heading": False,
                "prose_size": 0,
                "paragraphs": 0,
            }
        if tag == "a" and not hidden:
            href = (attributes.get("href") or "").strip().lower()
            if href and not href.startswith(("#", "javascript:", "mailto:", "tel:")):
                self.heading_links.add(self.nodes)
        if tag == "p" and not hidden:
            for node_id in self.node_ids:
                if node_id in self.cards:
                    self.cards[node_id]["paragraphs"] += 1
        if tag in self.BLOCK and not hidden:
            self._append("\n")
        region = None
        if tag in {"article", "main"} and not hidden:
            region = len(self.regions)
            self.regions.append((tag, [], []))
        if tag not in self.VOID:
            self.stack.append((tag, hidden, region))
            self.node_ids.append(self.nodes)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                if tag in self.BLOCK and not self.stack[index][1]:
                    self._append("\n")
                del self.stack[index:]
                del self.node_ids[index:]
                break

    def handle_data(self, data):
        _remaining(self.deadline, self.clock)
        if any(tag == "title" for tag, _, _ in self.stack):
            self.title.append(data)
            return
        if not self.stack or not self.stack[-1][1]:
            self._append(data)

    def body(self):
        if self.result_listing or self._repeated_result_cards():
            return "", "visible_text"
        for kind in ("article", "main"):
            candidates = [
                _clean_text("".join(parts))
                for tag, parts, non_link in self.regions
                if tag == kind and self._has_prose(parts, non_link)
            ]
            if candidates:
                text = max(candidates, key=len)
                if len(text) >= 80:
                    return text, kind
        text = _clean_text("".join(self.visible)) if self._has_prose(self.visible, self.non_link) else ""
        return text, "visible_text"

    def _repeated_result_cards(self):
        groups = {}
        for card in self.cards.values():
            if card["linked_heading"] and 80 <= card["prose_size"] <= 2000 and 1 <= card["paragraphs"] <= 3:
                groups.setdefault(card["parent"], []).append(card)
        total_prose = len(re.sub(r"\s", "", "".join(self.non_link)))
        return any(
            len(cards) >= 2 and sum(card["prose_size"] for card in cards) >= total_prose * 0.7
            for cards in groups.values()
        )

    @staticmethod
    def _has_prose(parts, non_link):
        text = _clean_text("".join(parts))
        prose = _clean_text("".join(non_link))
        # Link/card lists and navigation cannot supply the minimum body. A
        # genuine div-only article remains usable without article/main markup.
        return (
            len(prose) >= 80
            and len(prose) >= len(text) * 0.5
            and any(len(re.sub(r"\s+", " ", paragraph).strip()) >= 80 for paragraph in "".join(non_link).splitlines())
        )


class PublicSourceReader:
    """Fetch one public source once. Dependency injection never disables peer checks."""

    TOTAL_SECONDS = 8.0
    MAX_BYTES = 512 * 1024
    MAX_TEXT_CHARS = 24000
    MAX_REDIRECTS = 4
    _SENSITIVE = re.compile(
        r"token|secret|password|passwd|credential|authorization|apikey|signature|session|cookie|"
        r"^(?:auth|key|sig|jwt|ticket|code)$"
    )

    def __init__(self, *, resolver=None, transport_factory=None, clock=None):
        self.resolver = resolver or self._resolve
        self.transport_factory = transport_factory or _PinnedTransport
        self.clock = clock or time.monotonic

    @staticmethod
    def _resolve(host, port):
        return {item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}

    @classmethod
    def _url(cls, value):
        if not isinstance(value, str) or len(value) > 4096 or re.search(r"[\x00-\x20\x7f\\]", value):
            raise _ReadFailure("blocked", "unsafe_url")
        decoded = value
        for _ in range(3):
            decoded = unquote(decoded)
        if re.search(r"[\x00-\x1f\x7f\\]", decoded):
            raise _ReadFailure("blocked", "unsafe_url")
        try:
            parts = urlsplit(value)
            host = (parts.hostname or "").rstrip(".").encode("idna").decode("ascii").lower()
            port = parts.port
        except (ValueError, UnicodeError):
            raise _ReadFailure("blocked", "unsafe_url") from None
        if (
            parts.scheme.lower() not in {"http", "https"}
            or not host
            or parts.username is not None
            or parts.password is not None
        ):
            raise _ReadFailure("blocked", "unsafe_url")
        if port not in {None, 80, 443} or "%" in host or "@" in host:
            raise _ReadFailure("blocked", "unsafe_url")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise _ReadFailure("blocked", "non_public_address")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if "." not in host:
                raise _ReadFailure("blocked", "unsafe_url") from None
        else:
            _public_ip(host)
        # Decode the full query before parsing as well as keys: nested/encoded
        # separators must not disguise a credential parameter.
        query = parts.query
        for _ in range(4):
            for key, _value in parse_qsl(query, keep_blank_values=True, max_num_fields=100):
                normalized = re.sub(r"[^a-z0-9]", "", key.lower())
                tokens = re.findall(r"[a-z0-9]+", key.lower())
                if cls._SENSITIVE.search(normalized) or any(cls._SENSITIVE.search(token) for token in tokens):
                    raise _ReadFailure("blocked", "sensitive_query")
            expanded = unquote(query)
            if expanded == query:
                break
            query = expanded
        else:
            raise _ReadFailure("blocked", "ambiguous_query_encoding")
        authority = f"[{host}]" if ":" in host else host
        if port is not None:
            authority += f":{port}"
        return (
            urlunsplit((parts.scheme.lower(), authority, parts.path or "/", parts.query, "")),
            host,
            port or (443 if parts.scheme.lower() == "https" else 80),
        )

    def _addresses(self, host, port, deadline):
        answers = queue.Queue(maxsize=1)
        if not _DNS_ADMISSION.acquire(blocking=False):
            raise _ReadFailure("failed", "dns_capacity_exhausted")

        def resolve():
            try:
                answers.put((True, self.resolver(host, port)))
            except Exception:
                answers.put((False, None))
            finally:
                _DNS_ADMISSION.release()

        # Only DNS runs in this daemon. A timed-out result can never start HTTP.
        try:
            threading.Thread(target=resolve, daemon=True, name="public-source-dns").start()
        except RuntimeError:
            _DNS_ADMISSION.release()
            raise _ReadFailure("failed", "dns_capacity_exhausted") from None
        try:
            ok, addresses = answers.get(timeout=_remaining(deadline, self.clock))
        except queue.Empty:
            raise _ReadFailure("failed", "timeout") from None
        _remaining(deadline, self.clock)
        if not ok:
            raise _ReadFailure("failed", "dns_failed")
        if not addresses or len(addresses) > 64:
            raise _ReadFailure("blocked", "non_public_address")
        return {_public_ip(str(address)) for address in addresses}

    def _read_bytes(self, response, deadline):
        length = response.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > self.MAX_BYTES):
            raise _ReadFailure("failed", "response_too_large")
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in {"", "identity", "gzip", "deflate"}:
            raise _ReadFailure("blocked", "unsupported_content_encoding")
        decoder = (
            zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS)
            if encoding in {"gzip", "deflate"}
            else None
        )
        chunks, wire_size, decoded_size = [], 0, 0
        try:
            for raw in response.iter_raw():
                _remaining(deadline, self.clock)
                wire_size += len(raw)
                if wire_size > self.MAX_BYTES:
                    raise _ReadFailure("failed", "response_too_large")
                chunk = decoder.decompress(raw, self.MAX_BYTES - decoded_size + 1) if decoder else raw
                decoded_size += len(chunk)
                if decoded_size > self.MAX_BYTES or (decoder and decoder.unconsumed_tail):
                    raise _ReadFailure("failed", "decoded_body_too_large")
                chunks.append(chunk)
            if decoder and (not decoder.eof or decoder.unused_data):
                raise _ReadFailure("failed", "invalid_content_encoding")
        except zlib.error:
            raise _ReadFailure("failed", "invalid_content_encoding") from None
        _remaining(deadline, self.clock)
        return b"".join(chunks)

    @staticmethod
    def _decode_document(raw, response):
        charset = response.encoding or "utf-8"
        if len(charset) > 40:
            raise _ReadFailure("failed", "invalid_charset")
        try:
            return raw.decode(charset, errors="strict")
        except (UnicodeError, LookupError):
            raise _ReadFailure("failed", "invalid_charset") from None

    def _body(self, source, canonical_url, content_type, deadline):
        parts = urlsplit(canonical_url)
        search_paths = {"search", "search-results", "search_results", "results", "serp"}
        query_keys = {key.lower() for key, _ in parse_qsl(parts.query)}
        if set(parts.path.lower().split("/")) & search_paths and query_keys & {
            "q",
            "query",
            "keyword",
            "keywords",
            "search",
            "wd",
            "word",
            "s",
        }:
            raise _ReadFailure("blocked", "body_unavailable")
        if content_type == "text/plain":
            title, body, kind = None, _clean_text(source), "visible_text"
        else:
            parser = _BodyParser(deadline, self.clock)
            parser.feed(source)
            parser.close()
            title = _clean_text("".join(parser.title))[:300] or None
            body, kind = parser.body()
        if re.search(
            r"安全验证|验证码|访问验证|Just a moment|Attention Required|Access Denied|^(?:登录|用户登录|Sign in|Log in)(?:\s|$|[|\-—])",
            title or "",
            re.I,
        ) or re.search(
            r"请先登录|登录后(?:查看|阅读|继续)|登录.{0,8}查看全文|安全验证|完成.{0,8}验证码|"
            r"sign in to (?:continue|read)|log in to (?:continue|read)|checking your browser|enable javascript to continue",
            body,
            re.I,
        ):
            raise _ReadFailure("blocked", "access_gate")
        if len(re.sub(r"\s", "", body)) < 80:
            raise _ReadFailure("blocked", "body_unavailable")
        if len(body) > self.MAX_TEXT_CHARS:
            raise _ReadFailure("failed", "body_text_too_large")
        _remaining(deadline, self.clock)
        return title, body, kind

    @staticmethod
    def _public_url(url: str) -> str:
        return url

    def _validate_redirect(self, source_url: str, target_url: str) -> None:
        """Specialized readers can further narrow redirects; public checks remain per-hop."""

    @staticmethod
    def _request_url(url: str) -> tuple[str, dict]:
        return url, {}

    def _fetch_document(self, url: str, *, deadline: Optional[float] = None, observe_url=None) -> dict:
        """Internal bounded transport seam; raw HTML is never public body evidence."""
        result = {
            "status": "failed",
            "reason": None,
            "canonicalUrl": None,
            "htmlText": None,
            "contentType": None,
            "fetchedAt": "",
        }
        source_deadline = self.clock() + self.TOTAL_SECONDS
        deadline = min(source_deadline, deadline) if deadline is not None else source_deadline
        try:
            current = url
            visited = set()
            for hop in range(self.MAX_REDIRECTS + 1):
                _remaining(deadline, self.clock)
                canonical, host, port = self._url(current)
                if observe_url is not None:
                    observe_url(canonical)
                if canonical in visited:
                    raise _ReadFailure("blocked", "redirect_loop")
                visited.add(canonical)
                addresses = self._addresses(host, port, deadline)
                with ExitStack() as stack:
                    transport = stack.enter_context(
                        self.transport_factory(
                            host=host, port=port, addresses=addresses, deadline=deadline, clock=self.clock
                        )
                    )
                    client = stack.enter_context(
                        httpx.Client(
                            transport=transport,
                            trust_env=False,
                            follow_redirects=False,
                            timeout=httpx.Timeout(_remaining(deadline, self.clock), connect=3.0, read=4.0, write=3.0),
                            headers={
                                "User-Agent": "TripPublicSourceReader/1.0",
                                "Accept": "text/html,application/xhtml+xml,text/plain",
                                "Accept-Encoding": "identity",
                            },
                        )
                    )
                    request_url, request_extensions = self._request_url(canonical)
                    with client.stream("GET", request_url, extensions=request_extensions) as response:
                        _remaining(deadline, self.clock)
                        _validate_peer(response.extensions.get("network_stream"), addresses)
                        if (
                            len(response.headers) > 100
                            or sum(len(key) + len(value) for key, value in response.headers.multi_items()) > 32768
                        ):
                            raise _ReadFailure("failed", "response_headers_too_large")
                        if response.status_code in {301, 302, 303, 307, 308}:
                            if hop == self.MAX_REDIRECTS:
                                raise _ReadFailure("blocked", "redirect_limit")
                            location = response.headers.get("location")
                            if not location:
                                raise _ReadFailure("failed", "redirect_without_location")
                            current = urljoin(canonical, location)
                            self._validate_redirect(canonical, current)
                            continue
                        if response.status_code != 200:
                            raise _ReadFailure(
                                "blocked" if response.status_code in {401, 403, 429} else "failed",
                                f"http_{response.status_code}",
                            )
                        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
                            raise _ReadFailure("blocked", "unsupported_content_type")
                        raw = self._read_bytes(response, deadline)
                        source = self._decode_document(raw, response)
                        _remaining(deadline, self.clock)
                        result.update(
                            status="succeeded",
                            canonicalUrl=self._public_url(canonical),
                            htmlText=source,
                            contentType=content_type,
                        )
                        break
        except _ReadFailure as exc:
            result.update(status=exc.status, reason=exc.reason)
        except (httpx.TimeoutException, httpcore.TimeoutException, TimeoutError):
            result.update(status="failed", reason="timeout")
        except (httpx.HTTPError, httpcore.NetworkError, httpcore.ProtocolError, OSError):
            result.update(status="failed", reason="network_error")
        except (ValueError, TypeError, UnicodeError):
            result.update(status="failed", reason="invalid_response")
        if result["status"] != "succeeded":
            result.update(canonicalUrl=None, htmlText=None, contentType=None)
        result["fetchedAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return result

    def read(self, url: str, *, deadline: Optional[float] = None) -> PublicSourceReadResult:
        """The optional caller deadline uses the same monotonic clock as this reader."""
        source_deadline = self.clock() + self.TOTAL_SECONDS
        deadline = min(source_deadline, deadline) if deadline is not None else source_deadline
        document = self._fetch_document(url, deadline=deadline)
        result = {
            "status": document["status"],
            "reason": document["reason"],
            "canonicalUrl": None,
            "title": None,
            "bodyText": None,
            "contentFingerprint": None,
            "fetchedAt": document["fetchedAt"],
            "contentKind": None,
        }
        if document["status"] != "succeeded":
            return result
        try:
            title, body, kind = self._body(
                document["htmlText"], document["canonicalUrl"], document["contentType"], deadline
            )
            result.update(
                status="succeeded",
                canonicalUrl=document["canonicalUrl"],
                title=title,
                bodyText=body,
                contentFingerprint=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                contentKind=kind,
            )
        except _ReadFailure as exc:
            result.update(status=exc.status, reason=exc.reason)
        except (ValueError, TypeError, UnicodeError):
            result.update(status="failed", reason="invalid_response")
        result["fetchedAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return result
