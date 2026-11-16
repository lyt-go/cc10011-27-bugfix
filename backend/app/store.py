"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

为了模拟数据库事务，这里提供 ``transaction()``：进入时对全部表做快照，正常退出
才把改动整体切进去，任何异常都会回滚。分群画像的快照与来源记录必须在同一个
事务里落库，写入失败时两边都不能留下半条记录。
"""
from __future__ import annotations

import copy
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from app.seed import SEED_ROWS


class CommitError(RuntimeError):
    """提交阶段的写入失败：用于验证事务回滚，异常发生时所有改动都不会落库。"""


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._lock = threading.RLock()
        self._tx_depth = 0
        self._pending_failure: tuple[str | None, str] | None = None

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def find_by(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        for row in self.rows(module):
            if str(row.get(field)) == str(value):
                return row
        return None

    def next_id(self, module: str) -> int:
        return max((int(row.get("id", 0)) for row in self.rows(module)), default=0) + 1

    # ------------------------------------------------------------------
    # 事务与故障注入
    # ------------------------------------------------------------------

    def inject_commit_failure(self, reason: str = "模拟数据库提交失败", module: str | None = None) -> None:
        """让下一次事务在提交阶段失败，用于验证回滚后没有残缺版本。"""
        self._pending_failure = (module, reason)

    @contextmanager
    def transaction(self) -> Iterator["Store"]:
        """原子写入：所有改动先在副本上完成，提交成功后才对外可见。

        嵌套调用复用最外层事务，避免内层提前提交导致快照与来源记录不一致。
        """
        with self._lock:
            if self._tx_depth:
                self._tx_depth += 1
                try:
                    yield self
                finally:
                    self._tx_depth -= 1
                return

            backup = copy.deepcopy(self._tables)
            self._tx_depth = 1
            try:
                yield self
                pending, self._pending_failure = self._pending_failure, None
                if pending is not None:
                    raise CommitError(pending[1])
            except BaseException:
                self._tables = backup
                raise
            finally:
                self._tx_depth = 0

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
