import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from src.runtime.runtime_models import RuntimeErrorRecord


SECRET_KEY_RE = re.compile(r"(secret|token|cookie|password|api[_-]?key|authorization|credential)", re.IGNORECASE)
SECRET_VALUE_RE = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|[A-Za-z0-9_/-]{24,}\.[A-Za-z0-9_/-]{12,})")
LOCAL_ABSOLUTE_PATH_RE = re.compile(r"(?i)(?:\b[a-z]:[\\/][^\s\"']+|\\\\[^\\/\s\"']+[\\/][^\s\"']+)")
SAFE_TOKEN_METRIC_KEYS = {
    "prompttoken",
    "prompttokens",
    "completiontoken",
    "completiontokens",
    "totaltoken",
    "totaltokens",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_run_id() -> str:
    return f"run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex[:8]}"


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            normalized_key = normalize_key(str(key))
            if normalized_key == "tokenusage":
                redacted[key] = redact_token_usage(item)
                continue
            if normalized_key in SAFE_TOKEN_METRIC_KEYS:
                redacted[key] = item if is_non_negative_int(item) else "[redacted]"
                continue
            marker_value = isinstance(item, str) and item in {"present", "empty", "missing"}
            if is_secret_key(str(key)) and not marker_value:
                redacted[key] = "[redacted]"
            else:
                redacted[key] = redact(item)
        return redacted
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    if isinstance(value, str):
        if SECRET_VALUE_RE.search(value):
            return SECRET_VALUE_RE.sub("[redacted]", value)
        return value
    return value


def is_secret_key(key: str) -> bool:
    normalized = normalize_key(key)
    return normalized not in SAFE_TOKEN_METRIC_KEYS and bool(SECRET_KEY_RE.search(key))


def normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def redact_token_usage(value: Any) -> Any:
    if not isinstance(value, dict):
        return "[redacted]"
    return {
        key: item if normalize_key(str(key)) in SAFE_TOKEN_METRIC_KEYS and is_non_negative_int(item) else "[redacted]"
        for key, item in value.items()
    }


def redact_artifact(value: Any) -> Any:
    """Redact secrets and local paths only at the persisted-artifact boundary."""
    value = redact(value)
    if isinstance(value, dict):
        return {key: redact_artifact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_artifact(item) for item in value]
    if isinstance(value, str):
        return LOCAL_ABSOLUTE_PATH_RE.sub("[redacted-path]", value)
    return value


def redact_database_url(database_url: str) -> str:
    if database_url.startswith("sqlite:///"):
        name = Path(database_url.removeprefix("sqlite:///")).name or "sqlite"
        return f"sqlite:///[redacted]/{name}"
    if "://" in database_url:
        scheme = database_url.split("://", 1)[0]
        return f"{scheme}://[redacted]"
    return "[redacted]"


class RunArtifactWriter:
    def __init__(self, state_dir: Path, run_id: Optional[str] = None):
        self.run_id = run_id or make_run_id()
        self.run_dir = state_dir / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.files = {
            "manifest": "manifest.json",
            "input": "input.json",
            "context": "context.json",
            "agentPlan": "agent_plan.json",
            "agentDecision": "agent_decision.json",
            "agentObservations": "agent_observations.jsonl",
            "agentDecisions": "agent_decisions.jsonl",
            "agentActionOutcomes": "agent_action_outcomes.jsonl",
            "agentStop": "agent_stop.json",
            "planningSteps": "planning_steps.jsonl",
            "toolEvents": "tool_events.jsonl",
            "patches": "patches.jsonl",
            "portfolio": "portfolio.json",
            "planProposals": "plan_proposals.jsonl",
            "portfolioScores": "portfolio_scores.jsonl",
            "portfolioVerifier": "portfolio_verifier.jsonl",
            "portfolioSelection": "portfolio_selection.json",
            "finalResponse": "final_response.json",
            "sessionSnapshot": "session_snapshot.json",
            "verifierReport": "verifier_report.json",
            "itinerarySnapshot": "itinerary_snapshot.json",
            "errors": "errors.jsonl",
            "readme": "README.md",
        }

    def path(self, key: str) -> Path:
        return self.run_dir / self.files[key]

    def write_json(self, key: str, payload: Any) -> None:
        self.path(key).write_text(
            json.dumps(redact_artifact(payload), ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )

    def append_jsonl(self, key: str, items: list[Any]) -> None:
        path = self.path(key)
        with path.open("a", encoding="utf-8") as handle:
            for item in items:
                handle.write(json.dumps(redact_artifact(item), ensure_ascii=False, default=str) + "\n")

    def append_error(self, record: RuntimeErrorRecord) -> None:
        self.append_jsonl("errors", [record.model_dump(by_alias=True, exclude_none=True)])

    def ensure_jsonl_files(self) -> None:
        for key in ("planningSteps", "toolEvents", "patches", "errors", "agentObservations", "agentDecisions", "agentActionOutcomes"):
            self.path(key).touch(exist_ok=True)

    def artifact_files(self) -> dict[str, str]:
        return {key: name for key, name in self.files.items()}

    def as_posix_or_str(self) -> str:
        return os.fspath(self.run_dir)

    def absolute_path(self) -> str:
        return os.fspath(self.run_dir.resolve())


class RunArtifactReplayResult:
    def __init__(self, artifact_path: Path):
        self.artifact_path = artifact_path
        self.errors: list[dict[str, Any]] = []
        self.checked_files: list[str] = []
        self.final_response: dict[str, Any] = {}
        self.session_snapshot: dict[str, Any] = {}

    def add_error(self, file_name: str, message: str) -> None:
        self.errors.append({"file": file_name, "message": message})

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": "trip-ai-runtime-replay-v1",
            "status": "success" if not self.errors else "failed",
            "artifactPath": f"artifact://{self.artifact_path.name}",
            "checkedFiles": self.checked_files,
            "errors": self.errors,
            "sessionId": self.final_response.get("sessionId"),
            "activeVersionId": self.final_response.get("activeVersionId"),
        }


class RunArtifactReplayer:
    JSON_FILES = [
        "manifest.json",
        "input.json",
        "context.json",
        "agent_plan.json",
        "agent_decision.json",
        "agent_stop.json",
        "final_response.json",
        "session_snapshot.json",
        "verifier_report.json",
        "itinerary_snapshot.json",
    ]
    JSONL_FILES = [
        "planning_steps.jsonl",
        "tool_events.jsonl",
        "patches.jsonl",
        "errors.jsonl",
        "agent_observations.jsonl",
        "agent_decisions.jsonl",
        "agent_action_outcomes.jsonl",
    ]

    def replay(self, artifact_path: Path) -> dict[str, Any]:
        result = RunArtifactReplayResult(artifact_path)
        if not artifact_path.exists() or not artifact_path.is_dir():
            result.add_error(os.fspath(artifact_path), "artifact directory not found")
            return result.as_dict()
        for file_name in self.JSON_FILES:
            path = artifact_path / file_name
            if not path.exists():
                result.add_error(file_name, "required JSON file missing")
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                result.add_error(file_name, f"invalid JSON: {error}")
                continue
            result.checked_files.append(file_name)
            if file_name == "final_response.json":
                result.final_response = payload
            if file_name == "session_snapshot.json":
                result.session_snapshot = payload
        for file_name in self.JSONL_FILES:
            path = artifact_path / file_name
            if not path.exists():
                result.add_error(file_name, "required JSONL file missing")
                continue
            try:
                for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                    if line.strip():
                        json.loads(line)
            except json.JSONDecodeError as error:
                result.add_error(file_name, f"invalid JSONL at line {line_number}: {error}")
                continue
            result.checked_files.append(file_name)
        portfolio_files = ("portfolio.json", "portfolio_selection.json", "plan_proposals.jsonl", "portfolio_scores.jsonl", "portfolio_verifier.jsonl")
        if any((artifact_path / file_name).exists() for file_name in portfolio_files):
            for file_name in portfolio_files:
                path = artifact_path / file_name
                if not path.exists():
                    result.add_error(file_name, "portfolio artifact file missing")
                    continue
                try:
                    if path.suffix == ".jsonl":
                        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                            if line.strip():
                                json.loads(line)
                    else:
                        json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as error:
                    result.add_error(file_name, f"invalid portfolio JSON at line {locals().get('line_number', 0)}: {error}")
                    continue
                result.checked_files.append(file_name)
        final_version = result.final_response.get("activeVersionId")
        session = result.session_snapshot.get("conversation_session") or {}
        snapshot_version = session.get("active_version_id")
        if final_version != snapshot_version:
            result.add_error(
                "final_response.json",
                "activeVersionId does not match session_snapshot.conversation_session.active_version_id",
            )
        return result.as_dict()
