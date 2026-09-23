"""统一业务时钟。

所有业务时间一律取 ``now()``，便于在测试中固定或推进时间，
验证预约续期、过期释放等与时间相关的状态流。
"""
from datetime import datetime, timedelta
from typing import Optional

_fixed: Optional[datetime] = None


def now() -> datetime:
    """当前业务时间（UTC，naive，与库中既有时间字段保持一致）。"""
    if _fixed is not None:
        return _fixed
    return datetime.utcnow()


def set_fixed(value: Optional[datetime]) -> None:
    """固定时钟到指定时刻；传 None 恢复真实时钟。"""
    global _fixed
    _fixed = value


def reset() -> None:
    """恢复真实时钟。"""
    global _fixed
    _fixed = None


def advance(**kwargs) -> datetime:
    """推进固定时钟（如 minutes=...），返回推进后的时间。真实时钟下抛错。"""
    global _fixed
    if _fixed is None:
        raise RuntimeError("真实时钟下不能推进时间，请先 set_fixed")
    _fixed = _fixed + timedelta(**kwargs)
    return _fixed
