"""后台过期扫描。

守护线程周期性把到期未履约的预约置为 expired 并归还容量；
同时启动时（seed.init_db）也会立即扫描一次，因此服务重启后
待履约与已过期预约都能被继续识别。
"""
import logging
import threading
import time

from sqlalchemy.exc import SQLAlchemyError

from .config import RESERVATION_SWEEP_INTERVAL_SECONDS
from .database import SessionLocal
from .services.reservations import expire_due_reservations

logger = logging.getLogger("reservation-sweeper")


def sweep_once() -> int:
    """执行一轮过期释放，返回释放数量。任何异常都吞掉留给下一轮。"""
    db = SessionLocal()
    try:
        released = expire_due_reservations(db)
        db.commit()
        return released
    except SQLAlchemyError:
        db.rollback()
        logger.exception("预约过期扫描失败，将在下个周期重试")
        return 0
    finally:
        db.close()


class ReservationSweeper:
    """周期扫描线程（daemon，随进程退出）。"""

    def __init__(self, interval_seconds: int = RESERVATION_SWEEP_INTERVAL_SECONDS) -> None:
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="reservation-sweeper", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            sweep_once()
