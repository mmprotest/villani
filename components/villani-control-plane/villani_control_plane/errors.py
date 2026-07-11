class ServiceError(RuntimeError):
    status_code = 400
    code = "invalid_request"


class AuthenticationError(ServiceError):
    status_code = 401
    code = "authentication_required"


class AuthorizationError(ServiceError):
    status_code = 403
    code = "forbidden"


class NotFoundError(ServiceError):
    status_code = 404
    code = "not_found"


class ConflictError(ServiceError):
    status_code = 409
    code = "conflict"


class RateLimitError(ServiceError):
    status_code = 429
    code = "rate_limited"

    def __init__(self, message: str, retry_after: int = 60) -> None:
        super().__init__(message)
        self.headers = {"Retry-After": str(retry_after)}
