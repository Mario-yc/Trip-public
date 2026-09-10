"""Read an anonymously available, exact XHS note without executing JavaScript."""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import httpx

from src.services.public_source_reader import PublicSourceReader, _ReadFailure, _remaining


class _StateScripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.scripts = []
        self.visible = []
        self.script = None
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.script = [] if not dict(attrs).get("src") else None
            self.hidden += 1
        elif tag in {"style", "noscript", "svg"}:
            self.hidden += 1

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        elif not self.hidden:
            self.visible.append(data)

    def handle_endtag(self, tag):
        if tag == "script":
            if self.script is not None:
                self.scripts.append("".join(self.script))
            self.script = None
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden = max(0, self.hidden - 1)


class XiaohongshuPublicReader(PublicSourceReader):
    PARSER_VERSION = "xiaohongshu-public-ssr-v1"
    SHORT_HOSTS = {"xhslink.com", "xhslink.cn"}
    NOTE_HOSTS = {"www.xiaohongshu.com", "xiaohongshu.com"}
    NOTE_PATH = re.compile(r"/(?:discovery/item|explore)/([0-9a-f]{24})/?$")
    SHARE_KEYS = {"xsec_token", "xsec_source"}
    TRACKING_KEYS = {
        "app_platform",
        "app_version",
        "share_from_user_hidden",
        "type",
        "xhsshare",
        "shareredid",
        "apptime",
        "share_id",
        "sharetime",
        "author_share",
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_content",
        "source",
        "share_channel",
        "ignoreengage",
        "appuid",
    }
    MAX_IMAGES = 20
    MAX_STATE_CHARS = 256 * 1024

    @classmethod
    def _url(cls, value):
        try:
            parts = urlsplit(value)
            host = (parts.hostname or "").lower().rstrip(".")
            if host not in cls.SHORT_HOSTS | cls.NOTE_HOSTS:
                raise _ReadFailure("blocked", "xhs_domain_not_allowed")
            try:
                port = parts.port
            except ValueError:
                raise _ReadFailure("blocked", "xhs_port_not_allowed") from None
            if port not in {None, 443}:
                raise _ReadFailure("blocked", "xhs_port_not_allowed")
            if parts.scheme != "https":
                raise _ReadFailure("blocked", "xhs_https_required")
            pairs = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=64)
        except (ValueError, UnicodeError):
            raise _ReadFailure("blocked", "xhs_url_invalid") from None
        note_match = cls.NOTE_PATH.fullmatch(parts.path)
        if host in cls.NOTE_HOSTS and not note_match:
            raise _ReadFailure("blocked", "xhs_note_identity_mismatch")
        if host in cls.SHORT_HOSTS and not re.fullmatch(r"/[A-Za-z0-9/_-]{1,128}", parts.path):
            raise _ReadFailure("blocked", "xhs_url_invalid")
        kept = []
        seen = set()
        for key, val in pairs:
            if key in seen:
                raise _ReadFailure("blocked", "xhs_query_invalid")
            seen.add(key)
            if key in cls.SHARE_KEYS:
                if (
                    parts.scheme != "https"
                    or host != "www.xiaohongshu.com"
                    or parts.port not in {None, 443}
                    or not note_match
                    or parts.fragment
                ):
                    raise _ReadFailure("blocked", "xhs_share_scope_invalid")
                limit = 512 if key == "xsec_token" else 64
                pattern = r"[A-Za-z0-9_-]{1," + str(limit) + r"}" + (r"={0,2}" if key == "xsec_token" else "")
                if len(val) > limit or not re.fullmatch(pattern, val):
                    raise _ReadFailure("blocked", "xhs_query_invalid")
                kept.append((key, val))
            elif key.lower() not in cls.TRACKING_KEYS:
                raise _ReadFailure("blocked", "xhs_query_invalid")
        # Validate the complete original authority/path with the generic policy;
        # only the two narrowly scoped opaque share values bypass its query ban.
        clean = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        safe, safe_host, port = PublicSourceReader._url(clean)
        return safe + ("?" + urlencode(kept) if kept else ""), safe_host, port

    @staticmethod
    def _public_url(url):
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    @classmethod
    def _request_url(cls, url):
        # HTTPX logs Request.url. Keep share values only in the validated
        # transport target, so neither its INFO log nor Response.request leaks them.
        public_url = cls._public_url(url)
        return public_url, {"public_source_request_target": httpx.URL(url).raw_path}

    def _validate_redirect(self, source_url, target_url):
        self._url(target_url)
        source, target = urlsplit(source_url), urlsplit(target_url)
        original = self.NOTE_PATH.fullmatch(source.path)
        final = self.NOTE_PATH.fullmatch(target.path)
        if original and (not final or original.group(1) != final.group(1)):
            raise _ReadFailure("blocked", "xhs_redirect_identity_mismatch")
        if original and any(key in self.SHARE_KEYS for key, _ in parse_qsl(source.query)):
            if (source.scheme, source.netloc, source.path) != (target.scheme, target.netloc, target.path):
                raise _ReadFailure("blocked", "xhs_share_scope_invalid")

    @staticmethod
    def _leading_comments(script):
        while True:
            script = script.lstrip()
            if script.startswith("//"):
                end = script.find("\n")
                return "" if end < 0 else XiaohongshuPublicReader._leading_comments(script[end + 1 :])
            if script.startswith("/*"):
                end = script.find("*/", 2)
                if end < 0:
                    return ""
                script = script[end + 2 :]
                continue
            return script

    @classmethod
    def _state_json(cls, value, deadline, clock):
        if len(value) > cls.MAX_STATE_CHARS:
            raise _ReadFailure("failed", "xhs_state_too_large")
        value = value.strip()
        if value.endswith(";"):
            value = value[:-1].rstrip()
        if not value.startswith("{"):
            raise _ReadFailure("blocked", "xhs_state_invalid")
        out, index, depth, quoted, escaped, string_size = [], 0, 0, False, False, 0
        while index < len(value):
            if index % 1024 == 0:
                _remaining(deadline, clock)
            char = value[index]
            if quoted:
                string_size += 1
                if string_size > 65536:
                    raise _ReadFailure("failed", "xhs_state_too_large")
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
                out.append(char)
            elif char == '"':
                quoted, string_size = True, 0
                out.append(char)
            elif (
                value.startswith("undefined", index)
                and (index == 0 or not re.match(r"[\w$]", value[index - 1]))
                and (index + 9 == len(value) or not re.match(r"[\w$]", value[index + 9]))
            ):
                out.append("null")
                index += 8
            else:
                if char in "[{":
                    depth += 1
                elif char in "]}":
                    depth -= 1
                if not 0 <= depth <= 64:
                    raise _ReadFailure("blocked", "xhs_state_invalid")
                out.append(char)
            index += 1
        if depth or quoted:
            raise _ReadFailure("blocked", "xhs_state_invalid")

        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate_key")
                result[key] = value
            return result

        def reject_constant(_value):
            raise ValueError("invalid_constant")

        try:
            return json.loads("".join(out), object_pairs_hook=pairs, parse_constant=reject_constant)
        except (ValueError, RecursionError):
            raise _ReadFailure("blocked", "xhs_state_invalid") from None

    @staticmethod
    def _text(value):
        if not isinstance(value, str):
            raise _ReadFailure("blocked", "xhs_note_text_invalid")
        return re.sub(
            r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b\ufeff]", "", value.replace("\r\n", "\n").replace("\r", "\n")
        ).strip()

    @classmethod
    def _images(cls, raw):
        if not isinstance(raw, list) or len(raw) > cls.MAX_IMAGES:
            raise _ReadFailure("blocked", "xhs_image_metadata_invalid")
        images = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            value = item.get("urlDefault") or item.get("url")
            try:
                parsed = urlsplit(value) if isinstance(value, str) and len(value) <= 4096 else None
                host = (parsed.hostname or "").lower() if parsed else ""
                if (
                    not parsed
                    or parsed.scheme not in {"https", "http"}
                    or parsed.username is not None
                    or parsed.password is not None
                    or parsed.port not in {None, 80, 443}
                ):
                    continue
                if not (host.endswith(".xhscdn.com") or host.endswith(".xiaohongshu.com")):
                    continue
                width, height = item.get("width"), item.get("height")
                if (
                    type(width) is not int
                    or type(height) is not int
                    or not 0 < width <= 30000
                    or not 0 < height <= 30000
                ):
                    continue
                images.append(
                    {
                        "url": urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")),
                        "width": width,
                        "height": height,
                        "readStatus": "not_read",
                    }
                )
            except (ValueError, UnicodeError):
                continue
        return images

    @classmethod
    def extract_html(cls, html, *, canonical_url, deadline=None, clock=time.monotonic, sensitive_values=()):
        deadline = min(clock() + cls.TOTAL_SECONDS, deadline) if deadline is not None else clock() + cls.TOTAL_SECONDS
        observed_tokens = set(sensitive_values)
        result = {
            "status": "blocked",
            "reason": None,
            "canonicalUrl": None,
            "title": None,
            "bodyText": None,
            "contentFingerprint": None,
            "fetchedAt": "",
            "contentKind": None,
            "noteId": None,
            "images": [],
            "parserVersion": cls.PARSER_VERSION,
        }
        try:
            clean, _, _ = cls._url(canonical_url)
            observed_tokens.update(value for key, value in parse_qsl(urlsplit(clean).query) if key == "xsec_token")
            match = cls.NOTE_PATH.fullmatch(urlsplit(clean).path)
            if match is None:
                raise _ReadFailure("blocked", "xhs_note_identity_mismatch")
            if not isinstance(html, str) or len(html.encode("utf-8")) > cls.MAX_BYTES:
                raise _ReadFailure("failed", "response_too_large")
            parser = _StateScripts()
            parser.feed(html)
            parser.close()
            _remaining(deadline, clock)
            if re.search(
                r"登录后查看|请先登录|安全验证|验证码|Just a moment|Access Denied", " ".join(parser.visible), re.I
            ):
                raise _ReadFailure("blocked", "access_restricted")
            assignments = []
            for script in parser.scripts:
                script = cls._leading_comments(script)
                assignment = re.match(r"window\.__INITIAL_STATE__\s*=\s*", script)
                if assignment:
                    assignments.append(script[assignment.end() :])
            if len(assignments) != 1:
                raise _ReadFailure("blocked", "xhs_state_missing" if not assignments else "xhs_state_ambiguous")
            state = cls._state_json(assignments[0], deadline, clock)
            notes = state.get("note", {}).get("noteDetailMap", {})
            entry = notes.get(match.group(1), {}) if isinstance(notes, dict) else {}
            note = entry.get("note", {}) if isinstance(entry, dict) else {}
            if not isinstance(note, dict) or note.get("noteId") != match.group(1):
                raise _ReadFailure("blocked", "xhs_note_identity_mismatch")
            if isinstance(note.get("xsecToken"), str) and note["xsecToken"]:
                observed_tokens.add(note["xsecToken"])
            title, desc = cls._text(note.get("title", "")), cls._text(note.get("desc"))
            images = cls._images(note.get("imageList", []))
            result.update(
                canonicalUrl=cls._public_url(clean), title=title or None, noteId=match.group(1), images=images
            )
            if cls._contains_sensitive_text([title, desc, images, result["canonicalUrl"]], observed_tokens):
                raise _ReadFailure("blocked", "xhs_sensitive_content")
            if len(re.sub(r"\s", "", desc)) < 40:
                raise _ReadFailure("blocked", "xhs_note_text_unavailable")
            body = (title + "\n\n" if title else "") + desc
            if len(body) > cls.MAX_TEXT_CHARS:
                raise _ReadFailure("failed", "body_text_too_large")
            _remaining(deadline, clock)
            result.update(
                status="succeeded",
                bodyText=body,
                contentFingerprint=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                contentKind="article",
            )
        except _ReadFailure as exc:
            result.update(status=exc.status, reason=exc.reason)
        except (ValueError, TypeError, AttributeError, RecursionError):
            result.update(status="blocked", reason="xhs_state_invalid")
        if result["reason"] == "xhs_sensitive_content":
            result.update(
                canonicalUrl=None,
                title=None,
                bodyText=None,
                contentFingerprint=None,
                contentKind=None,
                noteId=None,
                images=[],
            )
        result["fetchedAt"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return result

    @staticmethod
    def _contains_sensitive_text(value, sensitive_values):
        text = json.dumps(value, ensure_ascii=False)
        for _ in range(3):
            if any(token and token in text for token in sensitive_values):
                return True
            decoded = unquote(text)
            if decoded == text:
                break
            text = decoded
        return False

    def read(self, url, *, deadline=None):
        deadline = (
            min(self.clock() + self.TOTAL_SECONDS, deadline)
            if deadline is not None
            else self.clock() + self.TOTAL_SECONDS
        )
        # Only this call owns these values, including values first seen in a
        # short-link redirect. Never persist or expose the observation set.
        sensitive_values = set()

        def observe_url(canonical):
            sensitive_values.update(value for key, value in parse_qsl(urlsplit(canonical).query) if key == "xsec_token")

        document = self._fetch_document(url, deadline=deadline, observe_url=observe_url)
        if document["status"] != "succeeded":
            return {
                "status": document["status"],
                "reason": document["reason"],
                "canonicalUrl": None,
                "title": None,
                "bodyText": None,
                "contentFingerprint": None,
                "fetchedAt": document["fetchedAt"],
                "contentKind": None,
                "noteId": None,
                "images": [],
                "parserVersion": self.PARSER_VERSION,
            }
        if document["contentType"] not in {"text/html", "application/xhtml+xml"}:
            document["htmlText"] = ""
        return self.extract_html(
            document["htmlText"],
            canonical_url=document["canonicalUrl"],
            deadline=deadline,
            clock=self.clock,
            sensitive_values=sensitive_values,
        )
