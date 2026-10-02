class AppError(Exception):
    """业务错误，携带面向用户的消息与 HTTP 状态码。"""

    def __init__(self, message, status=400, code=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code or self.__class__.__name__


class NotFoundError(AppError):
    def __init__(self, message="资源不存在"):
        super().__init__(message, status=404)


class ConflictError(AppError):
    def __init__(self, message="资源冲突"):
        super().__init__(message, status=409)


class PermissionError_(AppError):
    def __init__(self, message="无权执行此操作"):
        super().__init__(message, status=403)
