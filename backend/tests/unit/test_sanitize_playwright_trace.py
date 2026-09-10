import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[3] / "scripts" / "sanitize_playwright_trace.py"
SPEC = importlib.util.spec_from_file_location("sanitize_playwright_trace", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
sanitize_playwright_trace = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sanitize_playwright_trace)


def test_trace_sanitizer_replaces_known_credentials_and_sensitive_query_values(tmp_path, monkeypatch):
    monkeypatch.setenv("AMAP_JS_API_KEY", "amap-js-secret-value")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret-value")
    trace_path = tmp_path / "trace.zip"
    report_path = tmp_path / "trace-sanitization.json"
    with zipfile.ZipFile(trace_path, "w") as trace:
        trace.writestr(
            "trace.network",
            (
                "https://webapi.amap.com/maps?key=amap-js-secret-value&token=query-secret\n"
                '{"apiKey":"deepseek-secret-value","securityJsCode":"security-secret"}'
            ),
        )

    sanitize_playwright_trace.sanitize_trace(trace_path, report_path)

    with zipfile.ZipFile(trace_path, "r") as trace:
        payload = trace.read("trace.network").decode("utf-8")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert "amap-js-secret-value" not in payload
    assert "deepseek-secret-value" not in payload
    assert "query-secret" not in payload
    assert "security-secret" not in payload
    assert payload.count("[REDACTED]") == 4
    assert report["verifiedNoKnownCredentialValues"] is True
    assert report["verifiedNoSensitivePatterns"] is True
    assert report["completeCredentialInventoryVerified"] is False
    assert report["knownCredentialEnvNames"] == ["DEEPSEEK_API_KEY", "AMAP_JS_API_KEY"]
    assert report["knownCredentialValueCount"] == 2
    assert report["entryCount"] == 1
    assert report["changedEntryCount"] == 1
    assert report["replacementCount"] == 4


@pytest.mark.parametrize(
    "residual_payload",
    (
        b"Authorization: Bearer residual-bearer-value",
        b"https://example.test/resource?token=residual-query-value",
        b'{"apiKey":"residual-json-value"}',
    ),
)
def test_trace_sanitizer_fails_closed_when_any_sensitive_pattern_remains(
    tmp_path,
    monkeypatch,
    residual_payload,
):
    trace_path = tmp_path / "trace.zip"
    report_path = tmp_path / "trace-sanitization.json"
    with zipfile.ZipFile(trace_path, "w") as trace:
        trace.writestr("trace.network", residual_payload)

    monkeypatch.setattr(
        sanitize_playwright_trace,
        "sanitize_payload",
        lambda payload, _secrets: (payload, 0),
    )

    with pytest.raises(RuntimeError, match="Sensitive pattern remains in trace entry: trace.network"):
        sanitize_playwright_trace.sanitize_trace(trace_path, report_path)

    assert not report_path.exists()
    with zipfile.ZipFile(trace_path, "r") as trace:
        assert trace.read("trace.network") == residual_payload


def test_trace_sanitizer_rejects_empty_zip(tmp_path):
    trace_path = tmp_path / "trace.zip"
    report_path = tmp_path / "trace-sanitization.json"
    with zipfile.ZipFile(trace_path, "w"):
        pass

    with pytest.raises(RuntimeError, match="Playwright trace contains no entries"):
        sanitize_playwright_trace.sanitize_trace(trace_path, report_path)

    assert not report_path.exists()


def test_trace_sanitizer_does_not_claim_known_credential_closure_when_none_were_loaded(
    tmp_path,
    monkeypatch,
):
    for name in sanitize_playwright_trace.SECRET_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    trace_path = tmp_path / "trace.zip"
    report_path = tmp_path / "trace-sanitization.json"
    with zipfile.ZipFile(trace_path, "w") as trace:
        trace.writestr("trace.trace", b"benign trace payload")

    sanitize_playwright_trace.sanitize_trace(trace_path, report_path)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["entryCount"] == 1
    assert report["knownCredentialValueCount"] == 0
    assert report["knownCredentialEnvNames"] == []
    assert report["verifiedNoKnownCredentialValues"] is False
    assert report["verifiedNoSensitivePatterns"] is True
    assert report["completeCredentialInventoryVerified"] is False


def test_trace_sanitizer_cli_loads_repeated_runtime_env_files_without_parent_env(
    tmp_path,
    monkeypatch,
):
    for name in sanitize_playwright_trace.SECRET_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    deepseek_env_path = tmp_path / "deepseek.runtime.env"
    map_env_path = tmp_path / "map.runtime.env"
    deepseek_env_path.write_text(
        "DEEPSEEK_API_KEY=dotenv-deepseek-secret-value\n",
        encoding="utf-8",
    )
    map_env_path.write_text(
        "MAP_PROVIDER_KEY=dotenv-map-secret-value\n",
        encoding="utf-8",
    )
    trace_path = tmp_path / "trace.zip"
    report_path = tmp_path / "trace-sanitization.json"
    with zipfile.ZipFile(trace_path, "w") as trace:
        trace.writestr(
            "trace.trace",
            b"deepseek=dotenv-deepseek-secret-value map=dotenv-map-secret-value",
        )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT_PATH),
            "--trace",
            str(trace_path),
            "--report",
            str(report_path),
            "--runtime-env-file",
            str(deepseek_env_path),
            "--runtime-env-file",
            str(map_env_path),
        ],
    )

    assert sanitize_playwright_trace.main() == 0

    with zipfile.ZipFile(trace_path, "r") as trace:
        payload = trace.read("trace.trace").decode("utf-8")
    raw_report = report_path.read_text(encoding="utf-8")
    report = json.loads(raw_report)
    assert payload == "deepseek=[REDACTED] map=[REDACTED]"
    assert report["knownCredentialEnvNames"] == ["DEEPSEEK_API_KEY", "MAP_PROVIDER_KEY"]
    assert report["knownCredentialValueCount"] == 2
    assert report["verifiedNoKnownCredentialValues"] is True
    assert report["verifiedNoSensitivePatterns"] is True
    assert report["completeCredentialInventoryVerified"] is False
    assert "dotenv-deepseek-secret-value" not in raw_report
    assert "dotenv-map-secret-value" not in raw_report
    assert str(deepseek_env_path) not in raw_report
    assert str(map_env_path) not in raw_report


def test_log_sanitizer_redacts_sensitive_fields_and_unlabelled_known_values(
    tmp_path,
    monkeypatch,
):
    for name in sanitize_playwright_trace.SECRET_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    backend_env = tmp_path / "backend.runtime.env"
    root_env = tmp_path / "root.runtime.env"
    backend_env.write_text(
        "DEEPSEEK_API_KEY=dotenv-deepseek-log-value\n",
        encoding="utf-8",
    )
    root_env.write_text(
        "MAP_PROVIDER_SECURITY_JS_CODE=dotenv-security-log-value\n",
        encoding="utf-8",
    )
    raw_log = tmp_path / "backend.stdout.raw.log"
    safe_log = tmp_path / "backend.stdout.log"
    report_path = tmp_path / "log-sanitization.json"
    raw_log.write_text(
        (
            '{"jsApiKey":"inline-js-log-value",'
            '"securityJsCode":"dotenv-security-log-value"}\n'
            "unlabelled=dotenv-deepseek-log-value\n"
        ),
        encoding="utf-8",
    )

    sanitize_playwright_trace.sanitize_logs(
        ((raw_log, safe_log),),
        report_path,
        runtime_env_files=(backend_env, root_env),
    )

    safe_payload = safe_log.read_text(encoding="utf-8")
    raw_report = report_path.read_text(encoding="utf-8")
    report = json.loads(raw_report)
    assert not raw_log.exists()
    assert "inline-js-log-value" not in safe_payload
    assert "dotenv-security-log-value" not in safe_payload
    assert "dotenv-deepseek-log-value" not in safe_payload
    assert safe_payload.count("[REDACTED]") == 3
    assert report["fileCount"] == 1
    assert report["changedFileCount"] == 1
    assert report["verifiedNoKnownCredentialValues"] is True
    assert report["verifiedNoSensitivePatterns"] is True
    assert report["knownCredentialEnvNames"] == [
        "DEEPSEEK_API_KEY",
        "MAP_PROVIDER_SECURITY_JS_CODE",
    ]
    assert "dotenv-security-log-value" not in raw_report
    assert "dotenv-deepseek-log-value" not in raw_report
    assert str(backend_env) not in raw_report
    assert str(root_env) not in raw_report


def test_log_sanitizer_rechecks_written_output_and_fails_closed_on_residual(
    tmp_path,
    monkeypatch,
):
    raw_log = tmp_path / "backend.stderr.raw.log"
    safe_log = tmp_path / "backend.stderr.log"
    report_path = tmp_path / "log-sanitization.json"
    raw_log.write_bytes(b'{"securityJsCode":"residual-security-value"}')
    real_assert_sanitized = sanitize_playwright_trace.assert_payload_is_sanitized
    assertion_count = 0

    def fail_written_output_recheck(
        payload,
        secrets,
        entry_name,
        *,
        entry_kind="trace entry",
    ):
        nonlocal assertion_count
        assertion_count += 1
        real_assert_sanitized(
            payload,
            secrets,
            entry_name,
            entry_kind=entry_kind,
        )
        if assertion_count == 2:
            raise RuntimeError(f"Sensitive pattern remains in {entry_kind}: {entry_name}")

    monkeypatch.setattr(
        sanitize_playwright_trace,
        "assert_payload_is_sanitized",
        fail_written_output_recheck,
    )

    with pytest.raises(RuntimeError, match="Sensitive pattern remains in log file"):
        sanitize_playwright_trace.sanitize_logs(
            ((raw_log, safe_log),),
            report_path,
        )

    assert raw_log.exists()
    assert not safe_log.exists()
    assert not report_path.exists()
    assert assertion_count == 2


def test_log_sanitizer_reports_limited_closure_when_no_known_values_are_loaded(
    tmp_path,
    monkeypatch,
):
    for name in sanitize_playwright_trace.SECRET_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    raw_log = tmp_path / "frontend.stdout.raw.log"
    safe_log = tmp_path / "frontend.stdout.log"
    report_path = tmp_path / "log-sanitization.json"
    raw_log.write_text("benign log payload", encoding="utf-8")

    sanitize_playwright_trace.sanitize_logs(
        ((raw_log, safe_log),),
        report_path,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert safe_log.read_text(encoding="utf-8") == "benign log payload"
    assert report["knownCredentialValueCount"] == 0
    assert report["knownCredentialEnvNames"] == []
    assert report["verifiedNoKnownCredentialValues"] is False
    assert report["verifiedNoSensitivePatterns"] is True
    assert report["completeCredentialInventoryVerified"] is False


def test_log_sanitizer_cli_accepts_repeated_pairs_without_secret_arguments(
    tmp_path,
    monkeypatch,
):
    for name in sanitize_playwright_trace.SECRET_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    runtime_env = tmp_path / "runtime.env"
    runtime_env.write_text(
        "DEEPSEEK_API_KEY=dotenv-cli-log-value\n",
        encoding="utf-8",
    )
    first_raw = tmp_path / "first.raw.log"
    first_safe = tmp_path / "first.log"
    second_raw = tmp_path / "second.raw.log"
    second_safe = tmp_path / "second.log"
    report_path = tmp_path / "log-sanitization.json"
    first_raw.write_text("detached dotenv-cli-log-value", encoding="utf-8")
    second_raw.write_text(
        '{"securityJsCode":"inline-security-value"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT_PATH),
            "--log-pair",
            str(first_raw),
            str(first_safe),
            "--log-pair",
            str(second_raw),
            str(second_safe),
            "--report",
            str(report_path),
            "--runtime-env-file",
            str(runtime_env),
        ],
    )

    assert sanitize_playwright_trace.main() == 0

    assert first_safe.read_text(encoding="utf-8") == "detached [REDACTED]"
    assert second_safe.read_text(encoding="utf-8") == ('{"securityJsCode":"[REDACTED]"}')
    raw_report = report_path.read_text(encoding="utf-8")
    assert "dotenv-cli-log-value" not in raw_report
    assert "inline-security-value" not in raw_report
