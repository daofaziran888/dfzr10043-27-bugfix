"""业务写路径的互斥锁。

交接确认与回执落库会同时读写同一个批次的文件交付台账。HTTP 同步接口运行在
线程池里，迟到补件、旧确认重发和回执落库可能并发到达，必须按批次串行化，
否则同一个有效文件可能被两个请求同时判定为“尚未交付”。
"""
from __future__ import annotations

import threading

_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}


def lock_for(key: str) -> threading.RLock:
    """取得某个业务键（批次编号/明细编号）对应的可重入锁。"""
    with _guard:
        lock = _locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _locks[key] = lock
        return lock


# 文件交付台账是全局的：同一有效文件无论经由哪个批次的交接确认，都只能
# 交付一次，所以回执落库阶段还要再拿一把全局锁。
DELIVERY_LOCK_KEY = "file_drop:delivery"
