"""时钟抽象。

生产环境使用真实 UTC 时间；测试可冻结（freeze）或推进（tick）时间，
以便确定性地验证续期、过期等与时间相关的状态流转。
"""
from datetime import datetime, timedelta
from threading import Lock


class Clock:
    """支持固定时间的时钟（线程安全）。

    - 未冻结时 now() 返回真实 UTC 时间（naive datetime，与库内既有约定一致）；
    - freeze(t) 后 now() 恒等于 t，tick 可在冻结时间上推进。
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._fixed: datetime | None = None

    def now(self) -> datetime:
        with self._lock:
            return datetime.utcnow() if self._fixed is None else self._fixed

    def freeze(self, value: datetime) -> None:
        with self._lock:
            self._fixed = value

    def tick(self, **kwargs) -> datetime:
        """在冻结时间上推进（参数同 timedelta，如 minutes=10）；未冻结则先冻结到当前时间。"""
        with self._lock:
            if self._fixed is None:
                self._fixed = datetime.utcnow()
            self._fixed += timedelta(**kwargs)
            return self._fixed

    def reset(self) -> None:
        with self._lock:
            self._fixed = None


# 全局单例，业务代码统一从此取时间
clock = Clock()
