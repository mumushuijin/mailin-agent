class AppError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int = 400,
        *,
        code: str | None = None,
        payload: dict | None = None,
    ):
        self.message = message
        self.status_code = status_code
        self.code = code
        self.payload = payload or {}
        super().__init__(message)


class NotFoundError(AppError):
    def __init__(self, message: str = "资源不存在"):
        super().__init__(message, status_code=404)


class ConfigValidationError(AppError):
    def __init__(
        self,
        message: str = "配置校验失败",
        *,
        errors: list[dict] | None = None,
        revision: int | None = None,
        module: str | None = None,
    ):
        payload = {
            "code": "CONFIG_VALIDATION_ERROR",
            "errors": errors or [],
            "revision": revision,
            "module": module,
        }
        super().__init__(message, status_code=400, code="CONFIG_VALIDATION_ERROR", payload=payload)


class ConfigConflictError(AppError):
    def __init__(
        self,
        message: str = "配置已被其他请求更新，请重新加载后合并",
        *,
        revision: int,
        module: str | None = None,
        current: dict | None = None,
    ):
        payload = {
            "code": "CONFIG_REVISION_CONFLICT",
            "revision": revision,
            "module": module,
            "current": current,
        }
        super().__init__(message, status_code=409, code="CONFIG_REVISION_CONFLICT", payload=payload)
