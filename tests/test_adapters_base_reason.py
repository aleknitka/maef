from maef_api.adapters.base import model_error_reason
from maef_api.adapters.http import ModelHTTPError


def test_http_status_is_appended():
    assert model_error_reason(ModelHTTPError(429, "http_error")) == "model_error:ModelHTTPError:http_429"


def test_no_status_keeps_class_name_only():
    assert model_error_reason(TimeoutError("secret prompt text")) == "model_error:TimeoutError"
    assert model_error_reason(ModelHTTPError(None, "network_error")) == "model_error:ModelHTTPError"


def test_botocore_style_response_status():
    class AccessDeniedException(Exception):
        pass
    exc = AccessDeniedException("leaky text")
    exc.response = {"ResponseMetadata": {"HTTPStatusCode": 403}, "Error": {"Message": "leaky text"}}
    assert model_error_reason(exc) == "model_error:AccessDeniedException:http_403"


def test_message_text_never_included():
    reason = model_error_reason(ModelHTTPError(500, "sk-or-should-not-leak"))
    assert "sk-or" not in reason
