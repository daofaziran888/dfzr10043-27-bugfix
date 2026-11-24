"""交接时序回归：文件归集校验 → 批次确认 → 交接明细 → 迟到补件 → 回执。

复现题述场景：
1. 同一批次三份文件，第二份摘要不符；
2. 值班员按第一、第三份发起交接（明细只含能交接的两份）；
3. 接收回执尚未落库时，机器人迟到补报正确的第二份；
4. 旧确认被重发、同时取新明细，两条路径并发执行。

断言：无坏文件进入已接收统计、三份有效文件各实际交付一次、
原两件的明细与第二份坏摘要都能分别回查。
"""
from __future__ import annotations

import threading

import pytest

from app.dedup import sha256_hex
from app.services import service_for
from app.services.file_drop import manifest_checksum
from app.store import store


RUN_CODE = "FLOW-T-2026"


@pytest.fixture()
def scenario():
    """铺好一次采集批次：三份文件，第二份校验不符。"""
    file_svc = service_for("file_drop")
    batch_svc = service_for("capture_batch")

    batch, _ = batch_svc.create_entry({
        "批次编号": "CAPT-T1",
        "关联执行": RUN_CODE,
        "记录行数": "3",
        "采集窗口": "[2026-11-24]",
        "完整性校验": "",
        "批次标签": "file-collect",
        "上游来源": f"{RUN_CODE}:9001",
        "生成时间": "2026-11-24T09:00:00+08:00",
    })
    assert batch is not None

    slot_names = ["page-01.csv", "page-02.csv", "page-03.csv"]
    files = {}
    for order, name in enumerate(slot_names, start=1):
        values = {
            "归集编号": f"FILE-T{order}",
            "关联执行": RUN_CODE,
            "文件名称": name,
            "文件类型": "text/csv",
            "字节大小": "1024",
            "归集目录": "/captures/t1",
            "文件槽位": f"slot-{order}",
            "关联批次": "CAPT-T1",
        }
        if order == 2:
            # 第二份：机器人先报到一份摘要不符的坏文件。
            values["校验值"] = "bad-" + sha256_hex(name)
        files[order] = file_svc.register_file(values)
    return batch, files, slot_names


class TestLateFileHandoff:
    def test_batch_confirm_excludes_bad_file(self, scenario):
        batch, files, _ = scenario
        batch_svc = service_for("capture_batch")

        first = batch_svc.confirm_batch(batch["id"])
        assert first["ok"] is True
        assert first["file_count"] == 2
        assert first["valid_file_ids"] == [files[1]["id"], files[3]["id"]]
        assert first["excluded_file_ids"] == [files[2]["id"]]

        # 摘要按实际确认的两份有效文件计算，不是三份文件名的拼接。
        expected = manifest_checksum([files[1], files[3]])
        assert first["checksum"] == expected
        assert first["checksum"] != manifest_checksum(
            [files[1], files[2], files[3]])

        entry = batch_svc.get_entry(batch["id"])
        assert entry["status"] == "已确认"
        assert entry["确认摘要"] == expected
        assert [row["file_id"] for row in entry["有效文件明细"]] == [
            files[1]["id"], files[3]["id"]]
        # 坏文件留在排除明细里可复核，但不在确认集合中。
        assert [row["file_id"] for row in entry["排除明细"]] == [files[2]["id"]]
        assert entry["排除明细"][0]["校验值"].startswith("bad-")

    def test_handoff_manifest_uses_valid_files_only(self, scenario):
        batch, files, _ = scenario
        batch_svc = service_for("capture_batch")
        handoff_svc = service_for("handoff_pack")
        batch_svc.confirm_batch(batch["id"])

        opened = handoff_svc.open_handoff(
            "CAPT-T1", "datalake_downstream",
            file_ids=[files[1]["id"], files[3]["id"]],
            pack_code="HAND-T1")
        assert opened["ok"] is True
        assert opened["file_count"] == 2
        assert opened["file_ids"] == [files[1]["id"], files[3]["id"]]
        assert opened["checksum"] == manifest_checksum([files[1], files[3]])

        pack = handoff_svc.get_entry(opened["handoff_id"])
        assert pack["清单文件数"] == "2"
        assert {row["file_id"] for row in pack["发出明细"]} == {
            files[1]["id"], files[3]["id"]}
        # 坏文件没有被引用进清单。
        assert files[2]["id"] not in pack["发出文件ID"]

    def test_late_correct_file_and_parallel_replay(self, scenario):
        batch, files, slot_names = scenario
        batch_svc = service_for("capture_batch")
        file_svc = service_for("file_drop")
        handoff_svc = service_for("handoff_pack")

        batch_svc.confirm_batch(batch["id"])
        bad_checksum = batch_svc.get_entry(batch["id"])["完整性校验"]
        pack = "_".join([
            str(files[1]["id"]), str(files[3]["id"])])
        opened = handoff_svc.open_handoff(
            "CAPT-T1", "datalake_downstream",
            file_ids=[files[1]["id"], files[3]["id"]],
            pack_code="HAND-T1")
        pack_id = opened["handoff_id"]
        issued_snapshot = handoff_svc.issued_manifest(pack_id)
        assert issued_snapshot["checksum"] == bad_checksum

        # 旧确认尚未落回执时，机器人迟到补报正确的第二份。
        late = file_svc.record_late_file(
            RUN_CODE,
            {
                "归集编号": "FILE-T2-RETRY",
                "文件名称": "page-02.csv",
                "文件类型": "text/csv",
                "字节大小": "1024",
                "归集目录": "/captures/t1",
                "文件槽位": "slot-2",
                "替代文件": str(files[2]["id"]),
                "预期校验值": sha256_hex("page-02.csv"),
                "校验值": sha256_hex("page-02.csv"),
            },
            batch_code="CAPT-T1",
        )
        assert late["status"] == "已归集"

        # 补报后批次重新确认：三份都有效，摘要随之更新。
        reconfirmed = batch_svc.confirm_batch(batch["id"])
        assert reconfirmed["ok"] is True
        assert reconfirmed["file_count"] == 3
        assert files[2]["id"] not in reconfirmed["valid_file_ids"]
        assert late["id"] in reconfirmed["valid_file_ids"]
        assert reconfirmed["excluded_file_ids"] == [files[2]["id"]]

        results: dict[str, dict] = {}
        errors: list[BaseException] = []

        def replay_old_confirm():
            try:
                results["old"] = handoff_svc.confirm_handoff(pack_id)
            except BaseException as exc:  # pragma: no cover - 线程内异常兜底
                errors.append(exc)

        def fetch_new_manifest():
            try:
                results["new"] = batch_svc.valid_file_manifest(batch["id"])
            except BaseException as exc:  # pragma: no cover - 线程内异常兜底
                errors.append(exc)

        threads = [threading.Thread(target=replay_old_confirm),
                   threading.Thread(target=fetch_new_manifest)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors
        assert results["old"]["ok"] is True
        assert results["old"]["delivered_file_ids"] == [
            files[1]["id"], files[3]["id"]]
        assert results["new"]["file_count"] == 3

        # 新交接取补报后的三份有效文件，坏文件不在其中。
        new_pack = handoff_svc.open_handoff(
            "CAPT-T1", "datalake_downstream", pack_code="HAND-T1-B")
        assert new_pack["ok"] is True
        assert new_pack["file_ids"] == [
            files[1]["id"], late["id"], files[3]["id"]]
        confirmed = handoff_svc.confirm_handoff(new_pack["handoff_id"])
        assert confirmed["ok"] is True
        # 第一、第三份已由旧回执交付，新交接只补第二份。
        assert confirmed["delivered_file_ids"] == [late["id"]]
        assert set(confirmed["duplicate_file_ids"]) == {
            files[1]["id"], files[3]["id"]}

        # 已接收统计：无坏文件，三份有效文件各交付一次。
        summary = handoff_svc.delivered_summary("CAPT-T1")
        assert summary["invalid_delivered_file_ids"] == []
        assert summary["duplicated_file_ids"] == []
        assert summary["delivered_file_ids"] == sorted(
            [files[1]["id"], files[3]["id"], late["id"]])
        assert all(count == 1 for count in summary["delivery_count"].values())

        batch_entry = batch_svc.get_entry(batch["id"])
        assert files[2]["id"] not in batch_entry["已接收文件ID"]
        assert sorted(batch_entry["已接收文件ID"]) == sorted(
            [files[1]["id"], files[3]["id"], late["id"]])
        assert batch_entry["已接收文件数"] == "3"

        # 旧确认被重发：无论何时重放都幂等，不会把文件再交付一次。
        replayed = handoff_svc.confirm_handoff(pack_id)
        assert replayed["already_confirmed"] is True
        after = handoff_svc.delivered_summary("CAPT-T1")
        assert after["duplicated_file_ids"] == []

        # 原两件的明细与第二份坏摘要仍可分别复核。
        old_snapshot = handoff_svc.issued_manifest(pack_id)
        assert old_snapshot["checksum"] == bad_checksum
        assert old_snapshot["file_ids"] == [files[1]["id"], files[3]["id"]]
        assert old_snapshot["file_count"] == 2

        bad_rows = [
            row for row in store.rows("file_drop")
            if row.get("id") == files[2]["id"]
        ]
        assert bad_rows and bad_rows[0]["status"] == "校验不符"
        assert batch_entry["排除明细"][0]["校验值"] == bad_rows[0]["校验值"]

    def test_confirm_rejects_when_no_valid_files(self, scenario):
        batch, files, _ = scenario
        batch_svc = service_for("capture_batch")
        file_svc = service_for("file_drop")
        # 让第一、第三份也变成坏文件。
        for order in (1, 3):
            entry = file_svc.get_entry(files[order]["id"])
            entry["校验值"] = "bad-everything"

        result = batch_svc.confirm_batch(batch["id"])
        assert result["ok"] is False
        entry = batch_svc.get_entry(batch["id"])
        assert entry["status"] == "校验失败"
        assert entry["确认文件数"] == "0"
        assert entry["有效文件明细"] == []

    def test_receipt_cannot_extend_beyond_manifest_scope(self, scenario):
        batch, files, _ = scenario
        batch_svc = service_for("capture_batch")
        handoff_svc = service_for("handoff_pack")
        batch_svc.confirm_batch(batch["id"])
        opened = handoff_svc.open_handoff(
            "CAPT-T1", "datalake_downstream",
            file_ids=[files[1]["id"]],
            pack_code="HAND-T1-C")
        # 接收方试图顺带确认坏文件与范围外文件，均应被拒绝。
        result = handoff_svc.confirm_handoff(
            opened["handoff_id"],
            received_file_ids=[
                files[1]["id"], files[2]["id"], files[3]["id"]])
        assert result["ok"] is True
        assert result["delivered_file_ids"] == [files[1]["id"]]
        assert result["skipped_invalid_file_ids"] == []
        assert result["out_of_scope_file_ids"] == [
            files[2]["id"], files[3]["id"]]
        # 坏文件与范围外文件都不在本次确认覆盖范围内。
        assert result["covered_file_ids"] == [files[1]["id"]]
        assert files[3]["id"] not in result["received_file_ids"]


class TestBatchActionEntrypoints:
    """前端/路由层通过动作接口确认时也必须按有效文件取数。"""

    def test_confirm_action_uses_valid_files(self, scenario):
        batch, files, _ = scenario
        batch_svc = service_for("capture_batch")
        entry, message = batch_svc.run_action(batch["id"], "确认批次")
        assert entry is not None, message
        entry = batch_svc.get_entry(batch["id"])
        assert entry["status"] == "已确认"
        assert entry["确认文件数"] == "2"
        assert entry["排除明细"][0]["file_id"] == files[2]["id"]

    def test_handoff_confirm_action_lands_receipt_once(self, scenario):
        batch, files, _ = scenario
        batch_svc = service_for("capture_batch")
        handoff_svc = service_for("handoff_pack")
        batch_svc.confirm_batch(batch["id"])
        opened = handoff_svc.open_handoff(
            "CAPT-T1", "datalake_downstream", pack_code="HAND-T1-D")

        entry, message = handoff_svc.run_action(opened["handoff_id"], "确认交接")
        assert entry is not None, message
        assert entry["接收回执"]
        assert sorted(entry["接收文件ID"]) == sorted(
            [files[1]["id"], files[3]["id"]])

        entry2, message2 = handoff_svc.run_action(
            opened["handoff_id"], "确认交接")
        assert entry2 is not None, message2
        assert entry2["接收文件ID"] == entry["接收文件ID"]
        summary = handoff_svc.delivered_summary("CAPT-T1")
        assert summary["duplicated_file_ids"] == []

    def test_posted_handoff_computes_own_manifest(self, scenario):
        batch, files, _ = scenario
        batch_svc = service_for("capture_batch")
        handoff_svc = service_for("handoff_pack")
        batch_svc.confirm_batch(batch["id"])
        # 直接登记交接包：调用方自报的件数与摘要不可信，服务端重新取数。
        pack, _ = handoff_svc.create_entry({
            "交接编号": "HAND-T1-POST",
            "关联批次": "CAPT-T1",
            "目标系统": "datalake_downstream",
            "清单文件数": "3",
            "记录行数": "999",
            "校验摘要": "bogus",
            "交接版本": "v1",
            "交接时间": "2026-11-24T09:30:00+08:00",
        })
        assert pack is not None
        assert pack["清单文件数"] == "2"
        assert pack["校验摘要"] == manifest_checksum([files[1], files[3]])
        assert [row["file_id"] for row in pack["发出明细"]] == [
            files[1]["id"], files[3]["id"]]
