"""交接包服务：批次打包、清单校验与下游交接。

把已确认批次连同字段映射、校验摘要和清单打包成交接包，
供下游数仓直接消费并回溯来源。退回后批次不得再次交接。

交接清单只引用“有效文件明细”中的文件，并把发出时的明细与摘要
固化成快照：后续即便补进了正确文件，曾经发出的错误摘要和明细
仍然可以分别回查。接收方按明细所引用的范围处理已通过校验的文件，
回执写回归集记录，保证同一有效文件最多被确认交付一次。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.locks import DELIVERY_LOCK_KEY, lock_for
from app.services.base import BaseModuleService
from app.services.file_drop import (
    describe_file,
    is_verified,
    manifest_checksum,
    verify_file_rows,
)
from app.store import store


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def _receipt_no(pack_id: int) -> str:
    return f"RECEIPT-H{pack_id}"


def _as_int(value: Any) -> int:
    """宽松取整数：种子/历史数据里的行数可能不是纯数字。"""
    try:
        return int(str(value).strip() or "0")
    except (TypeError, ValueError):
        return 0


class HandoffPackService(BaseModuleService):
    MODULE = "handoff_pack"
    ENTITY = "交接包"
    OPERATION_TERM = "关联"
    LIST_FIELDS = [
        "交接编号", "关联批次", "目标系统", "清单文件数", "记录行数",
        "校验摘要", "交接版本", "交接时间",
    ]
    REQUIRED_FIELDS = ["交接编号", "关联批次", "目标系统"]
    STATUS_ORDER = ["待打包", "打包中", "已交接", "交接退回"]
    ACTION_RULES = {"启动打包": "打包中", "确认交接": "已交接", "记录退回": "交接退回"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """确认交接必须走确认流程并落回执，不能只改状态。

        前端动作与统一资源接口共用本方法；直接改状态会让交接“写到一半”，
        同一有效文件可能在重发时被再交付一次。
        """
        if action == "确认交接":
            result = self.confirm_handoff(entry_id)
            entry = self.get_entry(entry_id)
            if not result.get("ok"):
                return None, str(result.get("reason") or "交接确认失败")
            if result.get("already_confirmed"):
                return entry, f"交接包此前已确认，回执{result.get('receipt', '')}未重复落库"
            return entry, (
                f"交接已确认，接收{result.get('delivered_this_time', 0)}份有效文件，"
                f"回执{result.get('receipt', '')}"
            )
        return super().run_action(entry_id, action)

    # ── 内部工具 ─────────────────────────────────────────────

    def _batch_service(self):
        from app.services import service_for
        return service_for("capture_batch")

    def _file_service(self):
        from app.services import service_for
        return service_for("file_drop")

    def _batch_entry(self, batch_code: str) -> dict[str, Any] | None:
        batch_svc = self._batch_service()
        items, _ = batch_svc.list_entries(size=10000)
        for item in items:
            if item.get("批次编号") == batch_code:
                return item
        return None

    def _delivery_ledger(self) -> dict[int, str]:
        """文件交付台账：file_id -> 接收回执编号。"""
        ledger: dict[int, str] = {}
        for row in store.rows("file_drop"):
            receipt = str(row.get("交付回执", "") or "")
            if receipt:
                ledger[int(row.get("id", 0))] = receipt
        return ledger

    def _snapshot(self, batch_code: str,
                  file_ids: list[int] | None = None) -> dict[str, Any]:
        """按真实已验证文件生成可交接明细快照。"""
        batch = self._batch_entry(batch_code)
        if batch is None:
            return {
                "ok": False, "reason": "批次不存在", "files": [],
                "file_ids": [], "excluded_ids": [], "checksum": "", "count": 0,
            }
        run_code = batch.get("关联执行", "")
        verified, excluded = verify_file_rows(
            run_code, file_ids=file_ids, batch_code=batch_code)
        return {
            "ok": True,
            "batch": batch,
            "files": verified,
            "file_ids": [int(row["id"]) for row in verified],
            "excluded_ids": [int(row["id"]) for row in excluded],
            "checksum": manifest_checksum(verified),
            "count": len(verified),
        }

    def _snapshot_detail(self, files: list[dict[str, Any]]) -> list[dict[str, Any]]:
        detail = []
        for row in files:
            item = describe_file(row)
            detail.append(item)
        return detail

    # ── 打包 ─────────────────────────────────────────────────

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记交接包时按真实有效文件生成清单，不采信调用方自报的件数。

        通用资源接口直接 POST 交接包时也会走到这里：清单文件数、校验
        摘要和发出明细一律由批次执行下的已验证文件重新计算，保证页面
        明细与有效文件一一对应、坏文件不进入清单。
        """
        payload = dict(values)
        batch_code = str(payload.get("关联批次", "") or "")
        raw_ids = payload.get("发出文件ID")
        if raw_ids is None:
            raw_ids = payload.get("file_ids")
        file_ids = [int(fid) for fid in raw_ids] if raw_ids else None

        snapshot = self._snapshot(batch_code, file_ids=file_ids) if batch_code else None
        if snapshot and snapshot["ok"]:
            payload["清单文件数"] = str(snapshot["count"])
            payload["校验摘要"] = snapshot["checksum"]
            payload["记录行数"] = str(
                _as_int(snapshot["batch"].get("记录行数", "0"))
                * max(snapshot["count"], 1)
            )

        pack, missing = super().create_entry(payload)
        if pack is None:
            return pack, missing
        if snapshot and snapshot["ok"]:
            pack.update({
                "批次ID": snapshot["batch"].get("id"),
                "关联执行": snapshot["batch"].get("关联执行", ""),
                "发出明细": self._snapshot_detail(snapshot["files"]),
                "发出文件ID": list(snapshot["file_ids"]),
                "发出摘要": snapshot["checksum"],
                "排除文件ID": list(snapshot["excluded_ids"]),
                "批次确认摘要": snapshot["batch"].get("确认摘要", ""),
                "批次确认文件数": snapshot["batch"].get("确认文件数", "0"),
            })
        pack.setdefault("接收回执", "")
        pack.setdefault("接收文件ID", [])
        pack.setdefault("接收文件数", "0")
        pack.setdefault("重复文件ID", [])
        pack.setdefault("开启时间", _now_iso())
        return pack, []

    def open_handoff(self, batch_code: str, target_system: str, *,
                     file_ids: list[int] | None = None,
                     pack_code: str | None = None,
                     version: str = "v1") -> dict[str, Any]:
        """为已确认批次发起一次交接，固化发出时的明细与摘要快照。

        file_ids 是本次交接引用的文件范围；不传则取批次当前的全部有效
        文件。坏文件、未通过校验的文件无论是否在范围内都不会进入清单。
        """
        snapshot = self._snapshot(batch_code, file_ids=file_ids)
        if not snapshot["ok"]:
            return snapshot

        pack, missing = self.create_entry({
            "交接编号": pack_code or f"HAND-{batch_code}",
            "关联批次": batch_code,
            "目标系统": target_system,
            "交接版本": version,
            "交接时间": _now_iso(),
            "发出文件ID": list(snapshot["file_ids"]),
        })
        if pack is None:
            return {"ok": False, "reason": f"交接包缺少必填字段：{'、'.join(missing)}"}
        self.run_action(pack["id"], "启动打包")
        return {
            "ok": True,
            "handoff_id": pack["id"],
            "pack": pack,
            "checksum": pack.get("发出摘要", snapshot["checksum"]),
            "file_count": int(pack.get("清单文件数", "0") or "0"),
            "file_ids": list(pack.get("发出文件ID") or []),
            "excluded_file_ids": list(pack.get("排除文件ID") or []),
        }

    # ── 确认与回执 ───────────────────────────────────────────

    def confirm_handoff(self, pack_id: int, *,
                        received_file_ids: list[int] | None = None,
                        receipt: str | None = None) -> dict[str, Any]:
        """确认交接并落回执。

        幂等：已交接的包再次确认不会重复交付；同一有效文件如果已经
        由别的交接回执确认过，本次只跳过、不重复计入已接收统计。
        接收范围以包内发出明细引用的文件为准，不使用调用方额外扩大
        的范围，避免坏文件借道进入已接收统计。
        """
        pack = self.get_entry(pack_id)
        if pack is None:
            return {"ok": False, "reason": "交接包不存在"}

        batch_code = str(pack.get("关联批次", ""))
        lock = lock_for(f"batch:{batch_code or pack_id}")
        with lock:
            # 幂等重放：已经落过回执，直接返回原结果。
            if pack.get("status") == "已交接" and pack.get("接收回执"):
                return {
                    "ok": True,
                    "already_confirmed": True,
                    "handoff_id": pack_id,
                    "receipt": pack.get("接收回执", ""),
                    "delivered_file_ids": list(pack.get("接收文件ID") or []),
                    "received_file_count": int(pack.get("接收文件数", "0") or "0"),
                    "duplicate_file_ids": list(pack.get("重复文件ID") or []),
                }
            if pack.get("status") == "交接退回":
                return {
                    "ok": False,
                    "reason": "交接包已退回，不得再次交接",
                    "handoff_id": pack_id,
                }

            batch = self._batch_entry(batch_code)
            if batch is None:
                return {"ok": False, "reason": "交接包关联的批次不存在"}
            if batch.get("status") != "已确认":
                return {
                    "ok": False,
                    "reason": "批次尚未确认，不能交接",
                    "handoff_id": pack_id,
                }

            scope = [int(fid) for fid in (pack.get("发出文件ID") or [])]
            supplied = [int(fid) for fid in (received_file_ids or [])]
            out_of_scope: list[int] = []
            if supplied:
                # 接收方只能在发出明细引用的范围内收货；范围外文件既不
                # 接收也不记入“跳过”，保证覆盖范围始终可解释。
                scope_set = set(scope)
                out_of_scope = [fid for fid in supplied if fid not in scope_set]
                scope = [fid for fid in supplied if fid in scope_set]
            if not scope:
                return {
                    "ok": False,
                    "reason": "交接明细为空，没有可接收的有效文件",
                    "handoff_id": pack_id,
                    "checksum": pack.get("校验摘要", ""),
                    "out_of_scope_file_ids": out_of_scope,
                }

            run_code = str(pack.get("关联执行", "") or batch.get("关联执行", ""))
            verified, _excluded = verify_file_rows(
                run_code, file_ids=scope, batch_code=batch_code)
            eligible_ids = {int(row["id"]) for row in verified}

            receipt_no = receipt or _receipt_no(pack_id)
            delivered_now: list[int] = []
            duplicates: list[int] = []
            skipped_invalid: list[int] = []
            # 台账跨批次共享：锁住后再判定“是否已交付”，避免两个批次的
            # 交接同时把同一有效文件各确认一次。
            with lock_for(DELIVERY_LOCK_KEY):
                ledger = self._delivery_ledger()
                for fid in scope:
                    if fid not in eligible_ids:
                        skipped_invalid.append(fid)
                        continue
                    if fid in ledger:
                        duplicates.append(fid)
                        continue
                    row = store.find("file_drop", fid)
                    if row is None:
                        skipped_invalid.append(fid)
                        continue
                    row["交付回执"] = receipt_no
                    row["交付时间"] = _now_iso()
                    row["关联批次"] = row.get("关联批次") or batch_code
                    ledger[fid] = receipt_no
                    delivered_now.append(fid)

            # 接收明细是本次确认实际覆盖的文件（含此前已交付、本次复核的范围）。
            covered_rows = [
                describe_file(store.find("file_drop", fid))
                for fid in scope
                if fid in eligible_ids and store.find("file_drop", fid) is not None
            ]
            pack["接收回执"] = receipt_no
            pack["接收时间"] = _now_iso()
            pack["接收文件ID"] = delivered_now
            pack["接收文件数"] = str(len(delivered_now))
            pack["重复文件ID"] = duplicates
            pack["跳过文件ID"] = skipped_invalid
            pack["范围外文件ID"] = out_of_scope
            pack["本次覆盖文件ID"] = [row["file_id"] for row in covered_rows]
            pack["确认覆盖文件数"] = str(len(covered_rows))
            pack["接收明细"] = covered_rows
            # 清单文件数与校验摘要保持发出快照，不因本次收货范围而改写。
            super().run_action(pack_id, "确认交接")

            # 批次回执只累计实际交付的有效文件，不含任何坏文件。
            # 批次状态与回执写入同样在台账锁内完成，避免并发重放互相覆盖。
            with lock_for(DELIVERY_LOCK_KEY):
                batch_receipts = list(batch.get("接收记录") or [])
                batch_receipts.append({
                    "回执编号": receipt_no,
                    "交接编号": pack.get("交接编号", ""),
                    "文件ID": delivered_now,
                    "重复文件ID": duplicates,
                    "校验摘要": pack.get("校验摘要", ""),
                    "接收时间": pack.get("接收时间", ""),
                })
                batch["接收记录"] = batch_receipts
                received_union = sorted({
                    int(fid)
                    for item in batch_receipts
                    for fid in item.get("文件ID", [])
                })
                batch["接收回执"] = receipt_no
                batch["回执时间"] = pack.get("接收时间", "")
                batch["已接收文件数"] = str(len(received_union))
                batch["已接收文件ID"] = received_union
                batch["交付状态"] = "已交付" if received_union else "待交接"

            return {
                "ok": True,
                "already_confirmed": False,
                "handoff_id": pack_id,
                "batch_id": batch.get("id"),
                "receipt": receipt_no,
                "delivered_file_ids": delivered_now,
                "duplicate_file_ids": duplicates,
                "skipped_invalid_file_ids": skipped_invalid,
                "out_of_scope_file_ids": out_of_scope,
                "received_file_ids": received_union,
                "received_file_count": len(received_union),
                "delivered_this_time": len(delivered_now),
                "checksum": pack.get("校验摘要", ""),
                "covered_file_ids": [row["file_id"] for row in covered_rows],
            }

    def sync_from_batch(self, batch_code: str) -> dict[str, Any]:
        """把批次最近一次确认的结果写回交接清单。

        尚未发出的包（待打包）按最新有效文件刷新清单；已经发出或正在
        打包中的包只追加“批次确认摘要/确认文件数”供对照，发出时的明细与
        摘要快照保持不变，避免写到一半的交接被换掉文件范围。
        """
        batch = self._batch_entry(batch_code)
        if batch is None:
            return {"ok": False, "reason": "批次不存在"}
        confirmed_checksum = str(batch.get("确认摘要", "") or "")
        confirmed_count = str(batch.get("确认文件数", "0") or "0")
        refreshed: list[int] = []
        for pack in store.rows("handoff_pack"):
            if pack.get("关联批次") != batch_code:
                continue
            pack["批次确认摘要"] = confirmed_checksum
            pack["批次确认文件数"] = confirmed_count
            if (pack.get("接收回执")
                    or pack.get("status") in ("已交接", "交接退回", "打包中")):
                continue
            snapshot = self._snapshot(batch_code)
            if not snapshot["ok"]:
                continue
            pack["发出明细"] = self._snapshot_detail(snapshot["files"])
            pack["发出文件ID"] = list(snapshot["file_ids"])
            pack["发出摘要"] = snapshot["checksum"]
            pack["排除文件ID"] = list(snapshot["excluded_ids"])
            pack["清单文件数"] = str(snapshot["count"])
            pack["校验摘要"] = snapshot["checksum"]
            pack["记录行数"] = str(
                _as_int(batch.get("记录行数", "0")) * max(snapshot["count"], 1)
            )
            refreshed.append(int(pack["id"]))
        return {"ok": True, "refreshed_pack_ids": refreshed}

    # ── 查询与追溯 ───────────────────────────────────────────

    def issued_manifest(self, pack_id: int) -> dict[str, Any]:
        """回查曾发出的明细与摘要（补报后不会被覆盖）。"""
        pack = self.get_entry(pack_id)
        if pack is None:
            return {"ok": False, "reason": "交接包不存在"}
        return {
            "ok": True,
            "handoff_id": pack_id,
            "checksum": pack.get("发出摘要", pack.get("校验摘要", "")),
            "file_count": int(pack.get("清单文件数", "0") or "0"),
            "file_ids": list(pack.get("发出文件ID") or []),
            "files": list(pack.get("发出明细") or []),
            "excluded_file_ids": list(pack.get("排除文件ID") or []),
            "receipt": pack.get("接收回执", ""),
            "received_file_ids": list(pack.get("接收文件ID") or []),
            "received_files": list(pack.get("接收明细") or []),
        }

    def delivered_summary(self, batch_code: str | None = None) -> dict[str, Any]:
        """汇总实际已交付的有效文件，检查是否重复或无坏文件混入。"""
        packs = [
            pack for pack in store.rows("handoff_pack")
            if pack.get("status") == "已交接"
            and (not batch_code or pack.get("关联批次") == batch_code)
        ]
        deliveries: dict[int, list[int]] = {}
        for pack in packs:
            for fid in pack.get("接收文件ID") or []:
                deliveries.setdefault(int(fid), []).append(int(pack["id"]))
        invalid_delivered = [
            fid for fid in deliveries
            if not is_verified(store.find("file_drop", fid) or {})
        ]
        return {
            "delivered_file_ids": sorted(deliveries),
            "delivery_count": {
                fid: len(pack_ids) for fid, pack_ids in sorted(deliveries.items())
            },
            "duplicated_file_ids": sorted(
                fid for fid, pack_ids in deliveries.items() if len(pack_ids) > 1
            ),
            "invalid_delivered_file_ids": sorted(invalid_delivered),
            "handoff_ids": [int(pack["id"]) for pack in packs],
        }

    # ── 兼容既有调用 ─────────────────────────────────────────

    def reject_handoff(self, pack_id: int, reason: str) -> dict[str, Any]:
        """驳回交接包，退回后批次不得进入交接。"""
        entry = self.get_entry(pack_id)
        if entry is None:
            return {"ok": False, "reason": "交接包不存在"}
        if entry.get("status") == "已交接" and entry.get("接收回执"):
            return {"ok": False, "reason": "交接包已交付并落回执，不能退回"}
        self.run_action(pack_id, "记录退回")
        return {"ok": True, "pack_id": pack_id, "reason": reason, "action": "交接退回"}

    def get_delivered_packs(self, target_system: str | None = None) -> list[dict[str, Any]]:
        """获取已交接的包列表，可按目标系统筛选。"""
        items, _ = self.list_entries(size=10000)
        delivered = [it for it in items if it.get("status") == "已交接"]
        if target_system:
            delivered = [it for it in delivered if it.get("目标系统") == target_system]
        return delivered

    def valid_files_in_pack(self, pack_id: int) -> list[dict[str, Any]]:
        """按包内发出明细的范围返回其中仍通过校验的文件。"""
        pack = self.get_entry(pack_id)
        if pack is None:
            return []
        verified, _ = verify_file_rows(
            pack.get("关联执行", ""),
            file_ids=[int(fid) for fid in (pack.get("发出文件ID") or [])],
            batch_code=pack.get("关联批次", ""),
        )
        return verified
