from django.http import Http404
from rest_framework.exceptions import APIException, NotFound, ParseError, ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler


class ApiError(APIException):
    """Use this to return the contract's error shape with any status and code."""

    def __init__(self, status_code, code, message, details=None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or []
        super().__init__(message)


def _body(code, message, details):
    return {"error": {"code": code, "message": message, "details": details}}


def _flatten(detail):
    if isinstance(detail, dict):
        return [
            {"field": field, "message": " ".join(str(m) for m in (msgs if isinstance(msgs, list) else [msgs]))}
            for field, msgs in detail.items()
        ]
    if isinstance(detail, list):
        return [str(d) for d in detail]
    return [str(detail)]


def api_exception_handler(exc, context):
    if isinstance(exc, ApiError):
        return Response(_body(exc.code, exc.message, exc.details), status=exc.status_code)

    response = exception_handler(exc, context)
    if response is None:
        return None  # real server errors stay as they are

    if isinstance(exc, (NotFound, Http404)):
        response.data = _body("NOT_FOUND", "Resource not found.", [])
    elif isinstance(exc, (ValidationError, ParseError)):
        response.data = _body("VALIDATION_ERROR", "Invalid input.", _flatten(exc.detail))
    else:
        response.data = _body("ERROR", str(exc.detail), [])
    return response