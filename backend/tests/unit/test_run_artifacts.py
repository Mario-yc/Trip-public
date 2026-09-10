from src.runtime.run_artifacts import redact_artifact


def test_redact_artifact_preserves_token_counts_but_redacts_nested_credentials():
    result = redact_artifact(
        {
            "tokenUsage": {
                "prompt_tokens": 101,
                "completion_tokens": 22,
                "total_tokens": 123,
            },
            "refreshToken": {"nested": "must-not-survive"},
            "clientSecret": "must-not-survive",
        }
    )

    assert result["tokenUsage"] == {
        "prompt_tokens": 101,
        "completion_tokens": 22,
        "total_tokens": 123,
    }
    assert result["refreshToken"] == "[redacted]"
    assert result["clientSecret"] == "[redacted]"


def test_redact_artifact_token_metrics_fail_closed_for_non_numeric_values():
    result = redact_artifact(
        {
            "tokenUsage": {
                "prompt_tokens": "private-value",
                "completion_tokens": True,
                "total_tokens": -1,
                "opaque": "Bearer opaque-secret",
            },
            "prompt_tokens": "private-value",
            "tokenUsageScalar": "Bearer opaque-secret",
        }
    )

    assert result["tokenUsage"] == {
        "prompt_tokens": "[redacted]",
        "completion_tokens": "[redacted]",
        "total_tokens": "[redacted]",
        "opaque": "[redacted]",
    }
    assert result["prompt_tokens"] == "[redacted]"
    assert result["tokenUsageScalar"] == "[redacted]"
