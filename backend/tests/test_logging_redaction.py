from app.core.logging import redact_log_value


def test_redact_log_value_masks_sensitive_mapping_keys() -> None:
    result = redact_log_value(
        {
            "openai_api_key": "sk-secret",
            "nested": {"Authorization": "Bearer secret-token"},
            "message": "api_key=sk-inline token=inline-token",
        }
    )

    assert result["openai_api_key"] == "***"
    assert result["nested"]["Authorization"] == "***"
    assert result["message"] == "api_key=*** token=***"
