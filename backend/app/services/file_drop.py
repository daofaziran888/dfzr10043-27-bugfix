"""文件归集服务：中间文件收集、校验值索引与目录管理。

归集流程运行过程中下载或生成的中间文件，按执行实例建立
目录与校验值索引，校验不符的文件标记后供排查。

本模块同时负责“有效文件”的判定：只有归集完成且实际校验值等于
预期校验值的文件才算已验证文件。同一文件槽位如果先报错、后补报
了正确文件，只保留最新的已验证文件；坏文件永远不进入有效明细。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.dedup import sha256_hex
from app.services.base import BaseModuleService
from app.store import store

# 归集文件在通过校验后才允许进入交接明细。
VERIFIED_STATUS = "已归集"
INVALID_STATUS = "校验不符"


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def expected_checksum(row: dict[str, Any]) -> str:
    """文件应当具备的校验值。

    优先读取归集时登记的预期校验值；没有登记的历史记录按自身校验值
    视为自洽（种子数据没有独立的预期值可比对），登记时也会按文件名称
    计算默认预期值，与流水线 `_checksum` 保持同一算法。
    """
    declared = str(row.get("预期校验值", "") or "")
    if declared:
        return declared
    legacy = str(row.get("校验值", "") or "")
    if legacy:
        return legacy
    return sha256_hex(str(row.get("文件名称", "")))


def file_slot(row: dict[str, Any]) -> str:
    """文件槽位：同一逻辑文件在多次补报中保持同一个键。"""
    slot = str(row.get("文件槽位", "") or "").strip()
    if slot:
        return slot
    replaced = str(row.get("替代文件", "") or "").strip()
    if replaced:
        return replaced
    return str(row.get("文件名称", "") or "").strip()


def is_verified(row: dict[str, Any]) -> bool:
    """判断文件是否为真实通过完整性校验的有效文件。"""
    if row.get("status") != VERIFIED_STATUS:
        return False
    return str(row.get("校验值", "") or "") == expected_checksum(row)


def _row_expected(row: dict[str, Any]) -> str:
    """校验口径展示用：优先取登记的预期值，便于对照坏文件。"""
    declared = str(row.get("预期校验值", "") or "")
    if declared:
        return declared
    return sha256_hex(str(row.get("文件名称", "")))


def manifest_checksum(rows: list[dict[str, Any]]) -> str:
    """按实际确认的有效文件计算摘要。

    沿用批次原来的口径：确认文件的名称按槽位排序后用换行拼接，再取
    sha256 前 16 位。重复槽位在收集阶段已经去重，补报不会产生第二份
    摘要贡献；坏文件因为不在 rows 里也不会参与计算。
    """
    ordered = sorted(rows, key=lambda row: (file_slot(row), int(row.get("id", 0))))
    payload = "\n".join(str(row.get("文件名称", "") or "") for row in ordered)
    return sha256_hex(payload)


def _resolve_rows(
    run_code: str,
    *,
    file_ids: list[int] | None = None,
    batch_code: str = "",
) -> list[dict[str, Any]]:
    """收集属于本次确认范围的归集记录。

    以关联执行为主，同时纳入明确归属该批次的记录：迟到补件可能由另一
    次机器人执行补报，只要文件标注了批次就不能漏掉。批次归属优先，
    避免同一执行下其他批次的文件混入。
    """
    rows = [
        row for row in store.rows("file_drop")
        if row.get("关联执行") == run_code
        or (batch_code and str(row.get("关联批次", "") or "") == batch_code)
    ]
    scope = {int(fid) for fid in (file_ids or [])}
    if scope:
        rows = [row for row in rows if int(row.get("id", 0)) in scope]

    if batch_code:
        tagged = [
            row for row in rows
            if str(row.get("关联批次", "") or "") == batch_code
        ]
        untagged = [
            row for row in rows
            if not str(row.get("关联批次", "") or "")
        ]
        # 明确归属本批次的文件在前；未标批次的老数据按执行归集兼容。
        rows = tagged + untagged
    return rows


def verify_file_rows(
    run_code: str,
    *,
    file_ids: list[int] | None = None,
    batch_code: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """解析一次确认真正可以交接的文件。

    返回 (有效文件, 被排除文件)。有效文件按槽位去重，保留每个槽位
    最新的已验证记录；无效记录只在排除清单中留痕，不进入摘要。
    """
    verified_by_slot: dict[str, dict[str, Any]] = {}
    excluded: list[dict[str, Any]] = []
    for row in _resolve_rows(run_code, file_ids=file_ids, batch_code=batch_code):
        # 仍处于待归集的文件在取数时落定：校验值相符转为已归集，
        # 不符转为校验不符。这样“登记 → 确认批次”不会把已到位的好文件
        # 漏在外，也不会让坏文件留在待处理状态。
        if row.get("status") == "待归集":
            if str(row.get("校验值", "") or "") == expected_checksum(row):
                row["status"] = VERIFIED_STATUS
                row["校验结论"] = "通过"
            else:
                row["status"] = INVALID_STATUS
                row["校验结论"] = "不符"
        # 归集状态可能被外部改回，判定时按校验值重新确认一次。
        if row.get("status") == VERIFIED_STATUS and str(row.get("校验值", "") or "") != expected_checksum(row):
            row["status"] = INVALID_STATUS
            row["校验结论"] = "不符"
        if not is_verified(row):
            excluded.append(row)
            continue
        slot = file_slot(row)
        current = verified_by_slot.get(slot)
        # 同槽位多条有效记录时保留最新一条，避免补报后重复显示。
        if current is None or int(row.get("id", 0)) > int(current.get("id", 0)):
            verified_by_slot[slot] = row

    verified = sorted(
        verified_by_slot.values(),
        key=lambda row: (file_slot(row), int(row.get("id", 0))),
    )
    excluded = sorted(excluded, key=lambda row: int(row.get("id", 0)))
    return verified, excluded


def describe_file(row: dict[str, Any]) -> dict[str, Any]:
    """把归集记录转成交接明细中可追溯的一行。"""
    return {
        "file_id": int(row.get("id", 0)),
        "归集编号": row.get("归集编号", ""),
        "关联执行": row.get("关联执行", ""),
        "文件名称": row.get("文件名称", ""),
        "文件槽位": file_slot(row),
        "文件类型": row.get("文件类型", ""),
        "字节大小": row.get("字节大小", ""),
        "校验值": row.get("校验值", ""),
        "预期校验值": _row_expected(row),
        "校验结论": "通过",
        "关联批次": row.get("关联批次", ""),
        "补报": bool(row.get("补报")),
        "交付回执": row.get("交付回执", ""),
        "交付时间": row.get("交付时间", ""),
    }


def describe_excluded(row: dict[str, Any]) -> dict[str, Any]:
    """坏文件留痕：记录实际校验值与预期值，供分别复核。"""
    return {
        "file_id": int(row.get("id", 0)),
        "归集编号": row.get("归集编号", ""),
        "关联执行": row.get("关联执行", ""),
        "文件名称": row.get("文件名称", ""),
        "文件槽位": file_slot(row),
        "校验值": row.get("校验值", ""),
        "预期校验值": _row_expected(row),
        "校验结论": "不符",
        "status": row.get("status", ""),
        "关联批次": row.get("关联批次", ""),
        "补报": bool(row.get("补报")),
    }


class FileDropService(BaseModuleService):
    MODULE = "file_drop"
    ENTITY = "归集文件"
    OPERATION_TERM = "存储"
    LIST_FIELDS = [
        "归集编号", "关联执行", "文件名称", "文件类型", "字节大小",
        "校验值", "归集目录", "归集时间",
    ]
    REQUIRED_FIELDS = ["归集编号", "关联执行", "文件名称"]
    STATUS_ORDER = ["待归集", "已归集", "校验不符", "已清理"]
    ACTION_RULES = {"触发归集": "已归集", "校验文件": "校验不符", "清理归集": "已清理"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """执行文件动作；“校验文件”按真实校验值判定，不无条件判坏。

        归集列表里点校验时，只有校验值与预期值不一致才落 `校验不符`；
        相符的文件保持 `已归集`，避免好文件被误标记后从确认集合里消失。
        """
        if action == "校验文件":
            entry = self.get_entry(entry_id)
            if entry is None:
                return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
            expected_value = expected_checksum(entry)
            entry["预期校验值"] = expected_value
            actual = str(entry.get("校验值", "") or "")
            if actual == expected_value:
                entry["校验结论"] = "通过"
                return super().run_action(entry_id, "触发归集")
            entry["校验结论"] = "不符"
            return super().run_action(entry_id, action)
        return super().run_action(entry_id, action)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记归集文件时补齐校验口径字段。

        未提供校验值的文件按文件名称推导默认校验值（与流水线一致），
        同时记录文件槽位与预期校验值，使后续完整性取数有据可依。
        """
        payload = dict(values)
        name = str(payload.get("文件名称", "") or "")
        slot = str(payload.get("文件槽位") or payload.get("替代文件") or name)
        expected_value = str(payload.get("预期校验值") or "") or sha256_hex(name)
        payload.setdefault("归集编号", f"FILE-{slot or 'AUTO'}")
        entry, missing = super().create_entry(payload)
        if entry is None:
            return entry, missing
        entry["文件槽位"] = slot
        entry["预期校验值"] = expected_value
        entry["校验值"] = str(payload.get("校验值") or "") or expected_value
        entry["关联批次"] = str(payload.get("关联批次") or "")
        entry["补报"] = bool(payload.get("补报"))
        entry["替代文件"] = str(payload.get("替代文件") or "")
        entry["交付回执"] = ""
        entry["交付时间"] = ""
        return entry, []

    def verify_file(self, file_id: int, expected_checksum: str) -> dict[str, Any]:
        """校验文件完整性：比对校验值，不符则标记校验不符。"""
        entry = self.get_entry(file_id)
        if entry is None:
            return {"ok": False, "reason": "文件不存在"}
        actual = entry.get("校验值", "")
        entry["预期校验值"] = expected_checksum
        entry["校验结论"] = "通过" if actual == expected_checksum else "不符"
        if actual != expected_checksum:
            self.run_action(file_id, "校验文件")
            return {"ok": True, "file_id": file_id, "match": False}
        return {"ok": True, "file_id": file_id, "match": True}

    def get_files_by_run(self, run_code: str) -> list[dict[str, Any]]:
        """按关联执行获取归集文件列表。"""
        items, _ = self.list_entries(size=10000)
        return [it for it in items if it.get("关联执行") == run_code]

    def register_file(
        self,
        values: dict[str, Any],
        *,
        expected: str | None = None,
        late: bool = False,
    ) -> dict[str, Any]:
        """登记并校验一个归集文件，返回真实的归集记录。

        校验不依赖调用方自报的结论：登记后按预期校验值重新比对，
        只有匹配的文件才会保持 `已归集` 状态。
        """
        slot = str(values.get("文件槽位") or values.get("替代文件")
                   or values.get("文件名称", ""))
        expected_value = (
            expected
            or str(values.get("预期校验值") or "")
            or sha256_hex(str(values.get("文件名称", "")))
        )
        actual = str(values.get("校验值") or "") or expected_value
        payload = {
            "归集编号": values.get("归集编号") or f"FILE-{slot or 'AUTO'}",
            "关联执行": values.get("关联执行", ""),
            "文件名称": values.get("文件名称", ""),
            "文件类型": values.get("文件类型", "text/csv"),
            "字节大小": values.get("字节大小", "1024"),
            "校验值": actual,
            "归集目录": values.get("归集目录", ""),
            "归集时间": _now_iso(),
        }
        entry, missing = self.create_entry(payload)
        if entry is None:
            raise ValueError(f"归集文件缺少必填字段：{'、'.join(missing)}")
        entry.update({
            "文件槽位": slot,
            "预期校验值": expected_value,
            "关联批次": values.get("关联批次", ""),
            "补报": bool(late or values.get("补报")),
            "替代文件": values.get("替代文件", ""),
            "交付回执": "",
            "交付时间": "",
        })
        self.run_action(entry["id"], "触发归集")
        if actual != expected_value:
            entry["校验结论"] = "不符"
            # 明确按校验结果落状态，不依赖调用方传入的动作。
            self.run_action(entry["id"], "校验文件")
        else:
            entry["校验结论"] = "通过"
        return entry

    def record_late_file(self, run_code: str, values: dict[str, Any],
                         *, batch_code: str = "") -> dict[str, Any]:
        """机器人迟到补报：登记补件并作为同一槽位的最新有效文件。"""
        payload = dict(values)
        payload["关联执行"] = payload.get("关联执行") or run_code
        payload["关联批次"] = payload.get("关联批次") or batch_code
        payload.setdefault("补报", True)
        return self.register_file(payload, late=True)

    def valid_files(
        self,
        run_code: str,
        *,
        file_ids: list[int] | None = None,
        batch_code: str = "",
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """按执行/批次解析有效文件与排除文件。"""
        return verify_file_rows(run_code, file_ids=file_ids, batch_code=batch_code)

    def verified_manifest(self, run_code: str, *,
                          file_ids: list[int] | None = None,
                          batch_code: str = "") -> dict[str, Any]:
        """生成有效文件明细（不含坏文件），供批次确认和交接清单复用。"""
        verified, excluded = self.valid_files(
            run_code, file_ids=file_ids, batch_code=batch_code)
        return {
            "checksum": manifest_checksum(verified),
            "file_count": len(verified),
            "valid_file_ids": [int(row["id"]) for row in verified],
            "excluded_file_ids": [int(row["id"]) for row in excluded],
            "files": [describe_file(row) for row in verified],
            "excluded": [describe_excluded(row) for row in excluded],
        }

    def describe_file(self, row: dict[str, Any]) -> dict[str, Any]:
        """把归集记录转成交接明细中可追溯的一行。"""
        return describe_file(row)
