class DomainError(Exception):
    """领域错误基类。"""


class ValidationError(DomainError):
    def __init__(self, field: str, message: str):
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


class ConflictError(DomainError):
    """并发、幂等或唯一性冲突。"""


class NotFoundError(DomainError):
    """记录不存在。"""


class PermissionDenied(DomainError):
    """操作权限不足。"""


class RecoveryRequired(DomainError):
    """工作流处于需要恢复的中间状态。"""
