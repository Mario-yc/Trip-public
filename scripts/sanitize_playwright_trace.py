from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import time
import zipfile
from collections.abc import Sequence
from pathlib import Path

from dotenv import load_dotenv


SECRET_ENV_NAMES = (
    "DEEPSEEK_API_KEY",
    "MAP_JS_API_KEY",
    "AMAP_JS_API_KEY",
    "MAP_PROVIDER_KEY",
    "MAP_PROVIDER_SECURITY_JS_CODE",
    "AMAP_SECURITY_JS_CODE",
    "WEATHER_PROVIDER_KEY",
    "AMAP_WEB_SERVICE_KEY",
)

SENSITIVE_PATTERNS = (
    re.compile(rb"(?i)(Bearer\s+)(?!\[REDACTED\])[A-Za-z0-9._~+/=-]+"),
    re.compile(
        rb"(?i)([?&](?:key|api[_-]?key|js[_-]?api[_-]?key|security[_-]?js[_-]?code|"
        rb"token|signature|auth|secret)=)(?!\[REDACTED\])[^&#\s\"'<>]+"
    ),
    re.compile(
        rb"(?i)(\"?(?:api[_-]?key|js[_-]?api[_-]?key|security[_-]?js[_-]?code|"
        rb"token|secret|password)\"?\s*[:=]\s*\")"
        rb"(?!\[REDACTED\])[^\"]+"
    ),
    re.compile(
        rb"(?i)(\"?(?:api[_-]?key|js[_-]?api[_-]?key|security[_-]?js[_-]?code|"
        rb"token|secret|password)\"?\s*[:=]\s*)(?![\"\[])(?!\[REDACTED\])"
        rb"[^,;\s}\]]+"
    ),
)

LogPair = tuple[Path, Path]


def sanitize_payload(payload: bytes, secrets: list[bytes]) -> tuple[bytes, int]:
    replacements = 0
    for secret in secrets:
        count = payload.count(secret)
        if count:
            payload = payload.replace(secret, b"[REDACTED]")
            replacements += count
    for pattern in SENSITIVE_PATTERNS:
        payload, count = pattern.subn(rb"\1[REDACTED]", payload)
        replacements += count
    return payload, replacements


def assert_payload_is_sanitized(
    payload: bytes,
    secrets: list[bytes],
    entry_name: str,
    *,
    entry_kind: str = "trace entry",
) -> None:
    sensitive_pattern_matches = tuple(
        pattern.search(payload) is not None for pattern in SENSITIVE_PATTERNS
    )
    if any(sensitive_pattern_matches):
        raise RuntimeError(f"Sensitive pattern remains in {entry_kind}: {entry_name}")
    if any(secret in payload for secret in secrets):
        raise RuntimeError(f"Known credential remains in {entry_kind}: {entry_name}")


def load_known_credentials(
    runtime_env_files: Sequence[Path],
) -> tuple[list[str], list[bytes]]:
    for runtime_env_file in runtime_env_files:
        resolved_env_file = runtime_env_file.resolve()
        if not resolved_env_file.is_file():
            raise FileNotFoundError(
                f"Runtime environment file not found: {resolved_env_file}"
            )
        load_dotenv(resolved_env_file, override=False)
    known_credentials = [
        (name, value.encode("utf-8"))
        for name in SECRET_ENV_NAMES
        if (value := os.getenv(name, "").strip())
    ]
    return (
        [name for name, _value in known_credentials],
        [value for _name, value in known_credentials],
    )


def sanitize_trace(
    trace_path: Path,
    report_path: Path,
    runtime_env_files: Sequence[Path] = (),
) -> None:
    if not trace_path.is_file():
        raise FileNotFoundError(f"Playwright trace not found: {trace_path}")
    known_credential_env_names, secrets = load_known_credentials(runtime_env_files)
    with tempfile.NamedTemporaryFile(
        prefix=f"{trace_path.stem}-sanitized-",
        suffix=".zip",
        dir=trace_path.parent,
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)

    entry_count = 0
    changed_entry_count = 0
    replacement_count = 0
    try:
        with (
            zipfile.ZipFile(trace_path, "r") as source,
            zipfile.ZipFile(
                temporary_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
            ) as target,
        ):
            source_entries = source.infolist()
            if not source_entries:
                raise RuntimeError("Playwright trace contains no entries")
            for info in source_entries:
                entry_count += 1
                payload = source.read(info)
                sanitized, replacements = sanitize_payload(payload, secrets)
                assert_payload_is_sanitized(sanitized, secrets, info.filename)
                if replacements:
                    changed_entry_count += 1
                    replacement_count += replacements
                target.writestr(info, sanitized)
        for attempt in range(20):
            try:
                os.replace(temporary_path, trace_path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.25)
    finally:
        temporary_path.unlink(missing_ok=True)

    with zipfile.ZipFile(trace_path, "r") as sanitized_trace:
        sanitized_entries = sanitized_trace.infolist()
        if len(sanitized_entries) != entry_count or not sanitized_entries:
            raise RuntimeError("Sanitized Playwright trace entry count mismatch")
        for info in sanitized_entries:
            payload = sanitized_trace.read(info)
            assert_payload_is_sanitized(payload, secrets, info.filename)

    report = {
        "traceFile": trace_path.name,
        "entryCount": entry_count,
        "changedEntryCount": changed_entry_count,
        "replacementCount": replacement_count,
        "knownCredentialEnvNames": known_credential_env_names,
        "knownCredentialValueCount": len(secrets),
        "verifiedNoKnownCredentialValues": bool(secrets),
        "completeCredentialInventoryVerified": False,
        "verifiedNoSensitivePatterns": True,
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def sanitize_logs(
    log_pairs: Sequence[LogPair],
    report_path: Path,
    runtime_env_files: Sequence[Path] = (),
) -> None:
    if not log_pairs:
        raise ValueError("At least one raw/safe log pair is required")
    known_credential_env_names, secrets = load_known_credentials(runtime_env_files)
    prepared: list[tuple[Path, Path, Path, bool, int]] = []
    committed_safe_paths: list[Path] = []
    changed_file_count = 0
    replacement_count = 0
    try:
        for raw_path, safe_path in log_pairs:
            raw_path = raw_path.resolve()
            safe_path = safe_path.resolve()
            if raw_path == safe_path:
                raise ValueError("Raw and sanitized log paths must be different")
            if safe_path.exists():
                raise FileExistsError(f"Sanitized log already exists: {safe_path.name}")
            safe_path.parent.mkdir(parents=True, exist_ok=True)
            source_present = raw_path.is_file()
            payload = raw_path.read_bytes() if source_present else b""
            sanitized, replacements = sanitize_payload(payload, secrets)
            assert_payload_is_sanitized(
                sanitized,
                secrets,
                safe_path.name,
                entry_kind="log file",
            )
            with tempfile.NamedTemporaryFile(
                prefix=f"{safe_path.name}-sanitized-",
                suffix=".tmp",
                dir=safe_path.parent,
                delete=False,
            ) as temporary:
                temporary.write(sanitized)
                temporary_path = Path(temporary.name)
            prepared.append(
                (raw_path, safe_path, temporary_path, source_present, replacements)
            )
            if replacements:
                changed_file_count += 1
                replacement_count += replacements

        for (
            _raw_path,
            safe_path,
            temporary_path,
            _source_present,
            _replacements,
        ) in prepared:
            os.replace(temporary_path, safe_path)
            committed_safe_paths.append(safe_path)

        for (
            _raw_path,
            safe_path,
            _temporary_path,
            _source_present,
            _replacements,
        ) in prepared:
            assert_payload_is_sanitized(
                safe_path.read_bytes(),
                secrets,
                safe_path.name,
                entry_kind="log file",
            )

        for (
            raw_path,
            _safe_path,
            _temporary_path,
            source_present,
            _replacements,
        ) in prepared:
            if source_present:
                raw_path.unlink()

        report = {
            "schemaVersion": "trip-log-sanitization-v1",
            "fileCount": len(prepared),
            "sourceFileCount": sum(
                1 for _raw, _safe, _temporary, present, _count in prepared if present
            ),
            "changedFileCount": changed_file_count,
            "replacementCount": replacement_count,
            "knownCredentialEnvNames": known_credential_env_names,
            "knownCredentialValueCount": len(secrets),
            "verifiedNoKnownCredentialValues": bool(secrets),
            "completeCredentialInventoryVerified": False,
            "verifiedNoSensitivePatterns": True,
            "files": [
                {
                    "file": safe_path.name,
                    "sourcePresent": source_present,
                    "replacementCount": replacements,
                }
                for (
                    _raw_path,
                    safe_path,
                    _temporary_path,
                    source_present,
                    replacements,
                ) in prepared
            ],
        }
        encoded_report = json.dumps(report, ensure_ascii=False, indent=2).encode(
            "utf-8"
        )
        assert_payload_is_sanitized(
            encoded_report,
            secrets,
            report_path.name,
            entry_kind="log sanitization report",
        )
        report_path.write_bytes(encoded_report)
    except Exception:
        for safe_path in committed_safe_paths:
            safe_path.unlink(missing_ok=True)
        report_path.unlink(missing_ok=True)
        raise
    finally:
        for (
            _raw_path,
            _safe_path,
            temporary_path,
            _source_present,
            _replacements,
        ) in prepared:
            temporary_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sanitize Playwright traces or process logs without logging secrets."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--trace", type=Path)
    mode.add_argument(
        "--log-pair",
        action="append",
        default=[],
        nargs=2,
        metavar=("RAW_LOG", "SAFE_LOG"),
        type=Path,
        help="Raw and sanitized log paths; repeatable.",
    )
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument(
        "--runtime-env-file",
        action="append",
        default=[],
        type=Path,
        help="Runtime environment file to load without overriding the parent environment; repeatable.",
    )
    args = parser.parse_args()
    runtime_env_files = tuple(path.resolve() for path in args.runtime_env_file)
    if args.trace is not None:
        sanitize_trace(
            args.trace.resolve(),
            args.report.resolve(),
            runtime_env_files,
        )
    else:
        sanitize_logs(
            tuple((raw.resolve(), safe.resolve()) for raw, safe in args.log_pair),
            args.report.resolve(),
            runtime_env_files,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
