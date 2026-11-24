"""采集批次服务：执行产出归集、完整性校验、窗口去重和批次确认。

一次或多次流程执行的产出记录归集成批次，附带采集窗口和
完整性校验值供下游引用。去重逻辑按采集窗口+上游来源判重。

批次确认的口径是“实际通过校验的文件”：摘要只用有效文件计算，
校验不符的坏文件只留在归集记录中供复核，不进入确认集合，也不
会出现在交接明细里。补报后再次确认会重新取数，把最新的确认结果
写回交接清单。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.locks import lock_for
from app.services.base import BaseModuleService
from app.services.file_drop import (
    describe_excluded,
    describe_file,
    manifest_checksum,
    verify_file_rows,
)


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


class CaptureBatchService(BaseModuleService):
    MODULE = "capture_batch"
    ENTITY = "采集批次"
    OPERATION_TERM = "采集"
    LIST_FIELDS = [
        "批次编号", "关联执行", "记录行数", "采集窗口", "完整性校验",
        "批次标签", "上游来源", "生成时间",
    ]
    REQUIRED_FIELDS = ["批次编号", "关联执行", "记录行数"]
    STATUS_ORDER = ["归集中", "校验中", "已确认", "校验失败"]
    ACTION_RULES = {"启动校验": "校验中", "确认批次": "已确认", "标记失败": "校验失败"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """确认批次必须按真实有效文件取数，而不是直接把状态改成已确认。

        前端与统一资源接口都通过本方法触发动作，这里把“确认批次”导向
        完整性校验，避免坏文件残留在确认集合里。
        """
        if action == "确认批次":
            result = self.confirm_batch(entry_id)
            entry = self.get_entry(entry_id)
            if not result.get("ok"):
                return None, str(result.get("reason") or "批次确认失败")
            return entry, (
                f"采集批次已按{result.get('file_count', 0)}份有效文件确认，"
                f"摘要{result.get('checksum', '')}"
            )
        if action == "启动校验":
            base_entry, message = super().run_action(entry_id, action)
            if base_entry is not None:
                # 校验阶段只刷新明细与排除清单，已确认的摘要快照保持不动，
                # 等重新确认时才整体写回。
                self.refresh_manifest(entry_id)
            return base_entry, message
        return super().run_action(entry_id, action)

    def find_duplicate(self, checksum: str, source: str) -> dict[str, Any] | None:
        """按完整性校验值和上游来源查找重复批次。"""
        items, _ = self.list_entries(size=10000)
        for it in items:
            if it.get("完整性校验") == checksum and it.get("上游来源") == source:
                return it
        return None

    def get_confirmed_batches(self) -> list[dict[str, Any]]:
        """获取所有已确认的采集批次，供下游交接打包。"""
        items, _ = self.list_entries(size=10000)
        return [it for it in items if it.get("status") == "已确认"]

    def get_batch_by_code(self, batch_code: str) -> dict[str, Any] | None:
        items, _ = self.list_entries(size=10000)
        for item in items:
            if item.get("批次编号") == batch_code:
                return item
        return None

    # ── 有效文件明细 ─────────────────────────────────────────

    def valid_file_manifest(self, batch_id: int,
                            file_ids: list[int] | None = None) -> dict[str, Any]:
        """按真实已验证文件生成批次可交接明细。

        明细只包含“已归集且校验值相符”的文件；同一槽位的迟到补件会
        替换早先的坏文件，保证补报后不重复显示。坏文件列在 excluded
        中供复核，但不计入可接收范围。
        """
        entry = self.get_entry(batch_id)
        if entry is None:
            return {
                "ok": False, "reason": "批次不存在", "batch_id": batch_id,
                "checksum": "", "file_count": 0, "valid_file_ids": [],
                "excluded_file_ids": [], "files": [], "excluded": [],
            }
        verified, excluded = verify_file_rows(
            entry.get("关联执行", ""),
            file_ids=file_ids,
            batch_code=entry.get("批次编号", ""),
        )
        return {
            "ok": True,
            "batch_id": batch_id,
            "批次编号": entry.get("批次编号", ""),
            "checksum": manifest_checksum(verified),
            "file_count": len(verified),
            "record_count": len(verified) + len(excluded),
            "valid_file_ids": [int(row["id"]) for row in verified],
            "excluded_file_ids": [int(row["id"]) for row in excluded],
            "files": [describe_file(row) for row in verified],
            "excluded": [describe_excluded(row) for row in excluded],
        }

    def _apply_manifest(self, entry: dict[str, Any],
                        manifest: dict[str, Any]) -> None:
        """写回一次确认的完整结果（有效明细 + 排除明细 + 摘要快照）。"""
        entry["有效文件明细"] = manifest["files"]
        entry["排除明细"] = manifest["excluded"]
        entry["确认文件数"] = str(manifest["file_count"])
        entry["有效文件数"] = str(manifest["file_count"])
        entry["有效文件ID"] = list(manifest["valid_file_ids"])
        entry["排除文件数"] = str(len(manifest["excluded"]))
        entry["确认摘要"] = manifest["checksum"]
        entry["待确认变更"] = "否"

    def refresh_manifest(self, batch_id: int,
                         file_ids: list[int] | None = None) -> dict[str, Any]:
        """重新取数刷新有效文件明细（补报后、确认前）。

        只刷新明细与排除清单；已确认批次的摘要和确认文件数保持上次确认
        的快照，另以「待确认变更」提示需要重新确认，避免未确认的新文件
        被误当成已确认摘要的一部分。
        """
        manifest = self.valid_file_manifest(batch_id, file_ids=file_ids)
        if not manifest["ok"]:
            return manifest
        entry = self.get_entry(batch_id)
        assert entry is not None
        with lock_for(f"batch:{entry.get('批次编号', batch_id)}"):
            entry["有效文件明细"] = manifest["files"]
            entry["排除明细"] = manifest["excluded"]
            entry["排除文件数"] = str(len(manifest["excluded"]))
            if entry.get("status") == "已确认":
                pending = manifest["checksum"] != entry.get("确认摘要")
                entry["待确认变更"] = "是" if pending else "否"
            else:
                # 未确认的批次只更新待确认口径，确认摘要仍以确认为准。
                entry["确认文件数"] = str(manifest["file_count"])
                entry["有效文件数"] = str(manifest["file_count"])
                entry["有效文件ID"] = list(manifest["valid_file_ids"])
                entry["确认摘要"] = manifest["checksum"]
                entry["待确认变更"] = "是"
        return manifest

    # ── 校验与确认 ───────────────────────────────────────────

    def _sync_handoff(self, entry: dict[str, Any]) -> None:
        """把确认结果写回交接清单：未发出的包刷新，已发出的包留快照。"""
        from app.services import service_for
        handoff_svc = service_for("handoff_pack")
        if handoff_svc is not None and hasattr(handoff_svc, "sync_from_batch"):
            handoff_svc.sync_from_batch(str(entry.get("批次编号", "")))

    def verify_integrity(self, batch_id: int,
                         file_ids: list[int] | None = None) -> dict[str, Any]:
        """启动校验并按实际有效文件确认批次。

        摘要以真正通过完整性校验的文件为准，坏文件即使仍残留在归集
        列表里也不会进入确认集合。补报后再次确认会重新取数，并把最新
        的有效文件明细与摘要写回批次。
        """
        entry = self.get_entry(batch_id)
        if entry is None:
            return {"ok": False, "reason": "批次不存在"}
        self.run_action(batch_id, "启动校验")

        manifest = self.valid_file_manifest(batch_id, file_ids=file_ids)
        with lock_for(f"batch:{entry.get('批次编号', batch_id)}"):
            self._apply_manifest(entry, manifest)
            if manifest["file_count"] <= 0:
                entry["确认时间"] = ""
                entry["接收回执"] = ""
                super().run_action(batch_id, "标记失败")
                self._sync_handoff(entry)
                return {
                    "ok": False,
                    "reason": "没有通过完整性校验的文件，批次校验失败",
                    "batch_id": batch_id,
                    "checksum": manifest["checksum"],
                    "file_count": 0,
                    "excluded_file_ids": manifest["excluded_file_ids"],
                }
            entry["完整性校验"] = manifest["checksum"]
            entry["确认时间"] = _now_iso()
            # 直接落状态，避免再次进入 run_action("确认批次") 造成递归。
            super().run_action(batch_id, "确认批次")
            self._sync_handoff(entry)
            return {
                "ok": True,
                "batch_id": batch_id,
                "checksum": manifest["checksum"],
                "file_count": manifest["file_count"],
                "valid_file_ids": manifest["valid_file_ids"],
                "excluded_file_ids": manifest["excluded_file_ids"],
            }

    def confirm_batch(self, batch_id: int, *,
                      file_ids: list[int] | None = None,
                      operator: str = "system") -> dict[str, Any]:
        """确认批次：按有效文件重算摘要并写回确认清单。"""
        entry = self.get_entry(batch_id)
        if entry is None:
            return {"ok": False, "reason": "批次不存在"}
        result = self.verify_integrity(batch_id, file_ids=file_ids)
        if result.get("ok"):
            result["manifest"] = self.valid_file_manifest(
                batch_id, file_ids=file_ids)
            result["operator"] = operator
            result["确认时间"] = entry.get("确认时间", "")
        return result

    def confirmed_scope(self, batch_id: int) -> list[int]:
        """返回批次最近一次确认覆盖的文件 id，供交接追溯。"""
        entry = self.get_entry(batch_id)
        if entry is None:
            return []
        rows = entry.get("有效文件明细") or []
        return [int(row.get("file_id", 0)) for row in rows if row.get("file_id")]

    def mark_receipt(self, batch_id: int, receipt: str,
                     delivered_file_ids: list[int] | None = None) -> dict[str, Any]:
        """把接收回执写回批次，已接收统计只累计真实交付的有效文件。"""
        entry = self.get_entry(batch_id)
        if entry is None:
            return {"ok": False, "reason": "批次不存在"}
        ids = sorted({int(fid) for fid in (delivered_file_ids or [])})
        entry["接收回执"] = receipt
        entry["回执时间"] = _now_iso()
        entry["已接收文件数"] = str(len(ids))
        entry["已接收文件ID"] = ids
        entry["交付状态"] = "已交付" if ids else "待交接"
        return {
            "ok": True,
            "batch_id": batch_id,
            "receipt": receipt,
            "received_file_count": len(ids),
            "received_file_ids": ids,
        }
