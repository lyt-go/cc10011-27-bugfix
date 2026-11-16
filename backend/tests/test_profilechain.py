"""分群画像共享链路测试：定义保存、快照事务、版本幂等、冲突权衡与消费方一致性。

直接用服务层驱动，不依赖 HTTP 客户端；每个用例前重建仓库，避免互相污染。
"""
from __future__ import annotations

import threading
import unittest

from app import population
from app.seed import SEED_ROWS
from app.services import profilechain
from app.services.eventanalysis import EventanalysisService
from app.services.filterpreset import FilterpresetService
from app.services.segmentprofile import SegmentprofileService
from app.store import CommitError, Store, store


def reset_store() -> None:
    store._tables = {name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()}
    store._pending_failure = None
    store._tx_depth = 0


class ChainTestCase(unittest.TestCase):
    def setUp(self) -> None:
        reset_store()
        self.profiles = SegmentprofileService()
        self.presets = FilterpresetService()
        self.events = EventanalysisService()
        profilechain.bootstrap()

    def profile_row(self, entry_id: int) -> dict:
        return store.find(profilechain.PROFILE_MODULE, entry_id)

    def create_profile(self, **values) -> dict:
        payload = {
            "画像编号": "SEGM-T001",
            "分群名称": "测试分群",
            "筛选规则": "活跃度=高活跃",
            "画像维度": "城市,会员等级",
            "对比基准": "全量人群",
            "更新周期": "按周",
            **values,
        }
        entry, missing, message = self.profiles.create_entry(payload)
        self.assertIsNotNone(entry, message)
        return entry


class DefinitionPersistenceTests(ChainTestCase):
    def test_create_persists_rule_dimensions_baseline_cycle_and_coverage(self) -> None:
        entry = self.create_profile()
        stored = self.profile_row(int(entry["id"]))
        self.assertEqual(stored["筛选规则"], "活跃度=高活跃")
        self.assertEqual(stored["画像维度"], "城市、会员等级")
        self.assertEqual(stored["对比基准"], "全量人群")
        self.assertEqual(stored["更新周期"], "按周")
        expected = len(population.select("活跃度=高活跃", "2026-09-09~2026-09-15"))
        self.assertEqual(stored["覆盖对象数"], expected)
        self.assertEqual(stored["数据区间"], "2026-09-09~2026-09-15")
        self.assertTrue(stored["画像快照"].startswith("SNAP-"))

    def test_generated_coverage_is_reproducible(self) -> None:
        entry = self.create_profile()
        expected = len(population.select("活跃度=高活跃", "2026-09-09~2026-09-15"))
        self.assertEqual(entry["覆盖对象数"], expected)

    def test_client_supplied_coverage_is_recomputed(self) -> None:
        created = self.create_profile()
        entry, message = self.profiles.update_entry(int(created["id"]), {"覆盖对象数": 999999})
        self.assertIsNotNone(entry, message)
        self.assertEqual(entry["覆盖对象数"], created["覆盖对象数"])
        self.assertIn("已按同一快照重算", message)

    def test_preset_reference_uses_preset_rule_and_interval(self) -> None:
        entry = self.create_profile(预设编号="FILT-0002", 筛选规则=None, 对比基准="全量人群")
        self.assertEqual(entry["筛选规则"], "会员等级=金卡；注册渠道=广告投放")
        self.assertEqual(entry["数据区间"], "2026-09-09~2026-09-15")

    def test_preset_reference_rejects_conflicting_rule(self) -> None:
        entry, missing, message = self.profiles.create_entry(
            {
                "画像编号": "SEGM-T002",
                "分群名称": "冲突分群",
                "筛选规则": "活跃度=低活跃",
                "预设编号": "FILT-0002",
            }
        )
        self.assertIsNone(entry)
        self.assertIn("与预设 FILT-0002 的维度条件组不一致", message)

    def test_unknown_dimension_and_cycle_are_rejected(self) -> None:
        _, _, message = self.profiles.create_entry(
            {"画像编号": "SEGM-T003", "分群名称": "x", "筛选规则": "活跃度=高活跃", "画像维度": "不存在"}
        )
        self.assertIn("画像维度「不存在」不受支持", message)
        _, _, message = self.profiles.create_entry(
            {"画像编号": "SEGM-T004", "分群名称": "x", "筛选规则": "活跃度=高活跃", "更新周期": "按小时"}
        )
        self.assertIn("更新周期「按小时」不受支持", message)


class SnapshotVersionTests(ChainTestCase):
    def test_rule_change_recomputes_coverage_and_records_conflict(self) -> None:
        entry = self.create_profile()
        first = entry["覆盖对象数"]
        updated, message = self.profiles.update_entry(
            int(entry["id"]), {"筛选规则": "活跃度=高活跃；会员等级=金卡"}
        )
        self.assertIsNotNone(updated, message)
        self.assertNotEqual(updated["覆盖对象数"], first)
        self.assertEqual(updated["快照版本"], 2)

        history, _ = self.profiles.snapshot_history(int(entry["id"]))
        self.assertEqual([item["版本号"] for item in history], [2, 1])
        conflict = history[0]["口径冲突"]
        self.assertIn("筛选规则", conflict["变化项"])
        self.assertEqual(conflict["历史覆盖对象数"], first)
        self.assertEqual(conflict["现行覆盖对象数"], updated["覆盖对象数"])
        self.assertEqual(conflict["同区间判定"]["目标侧区间"], conflict["同区间判定"]["基准侧区间"])
        self.assertIn("不进入新对比结果", conflict["处理决定"])

    def test_cycle_change_rederives_interval_and_recomputes(self) -> None:
        entry = self.create_profile()
        weekly = entry["数据区间"]
        updated, message = self.profiles.update_entry(int(entry["id"]), {"更新周期": "按月"})
        self.assertIsNotNone(updated, message)
        self.assertNotEqual(updated["数据区间"], weekly)
        self.assertEqual(updated["数据区间"], population.resolve_interval("按月"))
        history, _ = self.profiles.snapshot_history(int(entry["id"]))
        self.assertIn("数据区间", history[0]["口径冲突"]["变化项"])
        self.assertEqual(
            updated["覆盖对象数"],
            len(population.select("活跃度=高活跃", updated["数据区间"])),
        )

    def test_explicit_interval_is_sticky_until_changed(self) -> None:
        entry = self.create_profile(数据区间="2026-09-01~2026-09-07")
        updated, message = self.profiles.update_entry(int(entry["id"]), {"更新周期": "按月"})
        self.assertIsNotNone(updated, message)
        self.assertEqual(updated["数据区间"], "2026-09-01~2026-09-07")

    def test_same_definition_does_not_open_new_version(self) -> None:
        entry = self.create_profile()
        before = len(self.profiles.snapshot_history(int(entry["id"]))[0])
        for _ in range(3):
            _, message = self.profiles.recompute(int(entry["id"]))
            self.assertIn("复用快照", message)
        history, _ = self.profiles.snapshot_history(int(entry["id"]))
        self.assertEqual(len(history), before)

    def test_concurrent_incremental_tasks_form_one_version(self) -> None:
        entry = self.create_profile()
        base_version = entry["快照版本"]
        results: list[dict] = []
        errors: list[BaseException] = []

        def worker(key: str) -> None:
            try:
                row, message = self.profiles.recompute(int(entry["id"]))
                results.append({"row": row, "message": message})
            except BaseException as exc:  # pragma: no cover - 失败时用于定位
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(str(index),)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(errors)
        history, _ = self.profiles.snapshot_history(int(entry["id"]))
        self.assertEqual(len(history), base_version)
        self.assertEqual(len({item["快照编号"] for item in history}), len(history))

    def test_idempotency_key_prevents_duplicate_versions(self) -> None:
        entry = self.create_profile()
        self.profiles.update_entry(int(entry["id"]), {"筛选规则": "活跃度=中活跃"})
        version_before = self.profile_row(int(entry["id"]))["快照版本"]
        first, message = self.profiles.recompute(int(entry["id"]), idempotency_key="job-77")
        self.assertIsNotNone(first, message)
        self.assertEqual(self.profile_row(int(entry["id"]))["快照版本"], version_before)
        # 同一任务重复投递：不新开版本
        again, message = self.profiles.recompute(int(entry["id"]), idempotency_key="job-77")
        self.assertIsNotNone(again, message)
        self.assertIn("复用快照", message)
        self.assertEqual(self.profile_row(int(entry["id"]))["快照版本"], version_before)
        history, _ = self.profiles.snapshot_history(int(entry["id"]))
        self.assertEqual(len([item for item in history if item["幂等键"] == "job-77"]), 1)

        # 同一幂等键换口径：明确拒绝，避免旧版本被顶替或覆盖
        self.profiles.update_entry(int(entry["id"]), {"筛选规则": "活跃度=低活跃"})
        rejected, message = self.profiles.recompute(int(entry["id"]), idempotency_key="job-77")
        self.assertIsNone(rejected)
        self.assertIn("幂等键 job-77 已用于版本", message)

    def test_published_profile_cannot_roll_back_or_change_definition(self) -> None:
        entry = self.create_profile()
        self.profiles.run_action(int(entry["id"]), "提交画像评审")
        self.profiles.run_action(int(entry["id"]), "发布画像")
        _, message = self.profiles.run_action(int(entry["id"]), "发布画像")
        self.assertIn("不允许执行「发布画像」", message)
        _, message = self.profiles.update_entry(int(entry["id"]), {"筛选规则": "活跃度=低活跃"})
        self.assertIn("不能直接回退", message)

        self.profiles.run_action(int(entry["id"]), "下线画像")
        _, message = self.profiles.run_action(int(entry["id"]), "发布画像")
        self.assertIn("可执行动作：无", message)
        _, message = self.profiles.recompute(int(entry["id"]))
        self.assertIn("已下线", message)

    def test_published_interval_is_frozen(self) -> None:
        entry = self.create_profile()
        self.profiles.run_action(int(entry["id"]), "提交画像评审")
        self.profiles.run_action(int(entry["id"]), "发布画像")
        _, message = self.profiles.recompute(int(entry["id"]), interval="2026-09-01~2026-09-07")
        self.assertIn("不能直接换区间重算", message)


class DifferenceTests(ChainTestCase):
    def test_difference_compares_same_interval_on_both_sides(self) -> None:
        entry = self.create_profile(对比基准="预设:FILT-0003")
        result, message = self.profiles.difference(int(entry["id"]))
        self.assertIsNotNone(result, message)
        check = result["画像差异"]["同区间校验"]
        self.assertTrue(check["一致"])
        self.assertEqual(result["画像差异"]["数据区间"], result["数据区间"])
        self.assertGreater(len(result["来源记录"]), 0)
        self.assertEqual(
            result["覆盖对象数"],
            len(population.select("活跃度=高活跃", result["数据区间"])),
        )
        self.assertEqual(
            result["基准覆盖对象数"],
            len(population.select("消费金额>=1000", result["数据区间"])),
        )

    def test_difference_recomputes_when_snapshot_is_stale(self) -> None:
        entry = self.create_profile()
        row = self.profile_row(int(entry["id"]))
        row["规则指纹"] = "drifted"
        result, message = self.profiles.difference(int(entry["id"]))
        self.assertIsNotNone(result, message)
        self.assertEqual(result["画像差异"]["数据区间"], row["数据区间"])
        self.assertNotEqual(self.profile_row(int(entry["id"]))["规则指纹"], "drifted")

    def test_snapshot_and_sources_are_written_together(self) -> None:
        entry = self.create_profile()
        snapshot = profilechain.get_snapshot_by_code(entry["画像快照"])
        self.assertIsNotNone(snapshot)
        source_types = {row["来源类型"] for row in snapshot["来源记录"]}
        self.assertEqual(source_types, {"目标人群数据", "分群筛选规则", "对比基准"})
        for row in snapshot["来源记录"]:
            self.assertEqual(row["数据区间"], snapshot["数据区间"])

    def test_transaction_failure_leaves_no_partial_version(self) -> None:
        entry = self.create_profile()
        versions_before = len(self.profiles.snapshot_history(int(entry["id"]))[0])
        snapshots_before = len(store.rows(profilechain.SNAPSHOT_MODULE))
        sources_before = len(store.rows(profilechain.SOURCE_MODULE))
        coverage_before = self.profile_row(int(entry["id"]))["覆盖对象数"]

        store.inject_commit_failure("模拟数据库写入失败")
        with self.assertRaises(CommitError):
            profilechain.compute_snapshot(
                int(entry["id"]), trigger="故障注入", interval="2026-09-01~2026-09-07"
            )

        self.assertEqual(len(store.rows(profilechain.SNAPSHOT_MODULE)), snapshots_before)
        self.assertEqual(len(store.rows(profilechain.SOURCE_MODULE)), sources_before)
        row = self.profile_row(int(entry["id"]))
        self.assertEqual(row["覆盖对象数"], coverage_before)
        self.assertEqual(row["快照版本"], 1)
        history, _ = self.profiles.snapshot_history(int(entry["id"]))
        self.assertEqual(len(history), versions_before)

    def test_difference_rejects_interval_outside_snapshot(self) -> None:
        entry = self.create_profile()
        self.profiles.run_action(int(entry["id"]), "提交画像评审")
        self.profiles.run_action(int(entry["id"]), "发布画像")
        result, message = self.profiles.difference(
            int(entry["id"]), interval="2026-09-01~2026-09-07"
        )
        self.assertIsNone(result)
        self.assertIn("不能直接换区间重算", message)

    def test_difference_recomputes_to_new_interval_for_draft(self) -> None:
        entry = self.create_profile()
        result, message = self.profiles.difference(
            int(entry["id"]), interval="2026-09-01~2026-09-07"
        )
        self.assertIsNotNone(result, message)
        self.assertEqual(result["数据区间"], "2026-09-01~2026-09-07")
        self.assertEqual(
            result["画像差异"]["同区间校验"]["基准侧区间"], "2026-09-01~2026-09-07"
        )


class ConsumerSyncTests(ChainTestCase):
    def test_event_analysis_consumes_latest_snapshot(self) -> None:
        result, message = self.events.comparison(3)
        self.assertIsNotNone(result, message)
        self.assertEqual(result["快照编号"], self.profile_row(3)["画像快照"])
        self.assertEqual(result["覆盖对象数"], self.profile_row(3)["覆盖对象数"])
        self.assertTrue(result["口径一致性"]["规则指纹一致"])

    def test_offline_profile_cannot_join_event_analysis(self) -> None:
        entry = self.create_profile()
        for action in ("提交画像评审", "发布画像", "下线画像"):
            self.profiles.run_action(int(entry["id"]), action)
        created, missing, message = self.events.create_entry(
            {"模型编号": "EVEN-T001", "事件名称": "x", "分组维度": "城市", "画像编号": entry["画像编号"]}
        )
        self.assertIsNone(created)
        self.assertIn("已下线", message)

        row = store.find("eventanalysis", 3)
        original = row["画像编号"]
        row["画像编号"] = entry["画像编号"]
        try:
            result, message = self.events.comparison(3)
            self.assertIsNone(result)
            self.assertIn("不能参与事件分析对比", message)
        finally:
            row["画像编号"] = original

    def test_offline_after_publish_blocks_existing_consumer(self) -> None:
        segment = store.find_by(profilechain.PROFILE_MODULE, "画像编号", "SEGM-0003")
        self.profiles.run_action(int(segment["id"]), "下线画像")
        result, message = self.events.comparison(3)
        self.assertIsNone(result)
        self.assertIn("已下线", message)

    def test_sync_brings_consumer_to_new_version(self) -> None:
        segment = store.find_by(profilechain.PROFILE_MODULE, "画像编号", "SEGM-0001")
        self.profiles.run_action(int(segment["id"]), "提交画像评审")
        self.profiles.run_action(int(segment["id"]), "发布画像")
        self.profiles.update_entry(int(segment["id"]), {"覆盖对象数": 0})  # 已发布 -> 拒绝
        view = self.events.get_entry(1)
        self.assertIn(view["快照同步状态"], {"已同步", "待同步"})
        entry, message = self.events.sync_snapshot(1)
        self.assertIsNotNone(entry, message)
        self.assertEqual(entry["快照编号"], self.profile_row(int(segment["id"]))["画像快照"])

    def test_preset_preview_matches_profile_coverage(self) -> None:
        first = self.create_profile(预设编号=None, 筛选规则="会员等级=金卡；注册渠道=广告投放")
        preview, message = self.presets.preview(2)
        self.assertIsNotNone(preview, message)
        self.assertEqual(preview["覆盖对象数"], first["覆盖对象数"])
        self.assertEqual(preview["数据区间"], first["数据区间"])

    def test_preset_rule_change_bumps_version_and_blocks_stale_reference(self) -> None:
        entry, message = self.presets.update_entry(1, {"维度条件组": "活跃度=中活跃"})
        self.assertIsNotNone(entry, message)
        self.assertEqual(entry["版本号"], 2)
        profile, message = self.profiles.update_entry(1, {"预设编号": "FILT-0001"})
        self.assertIsNone(profile)
        self.assertIn("不一致", message)

    def test_profile_flags_preset_version_drift(self) -> None:
        entry = self.create_profile(预设编号="FILT-0001", 筛选规则=None)
        self.assertEqual(entry["画像维度"], "城市、会员等级")
        self.presets.update_entry(1, {"维度条件组": "活跃度=中活跃"})
        view = self.profiles.get_entry(int(entry["id"]))
        self.assertEqual(view["快照状态"], "需重算")
        self.assertIn("FILT-0001", view["口径提示"])
        report = profilechain.consistency_report()
        self.assertTrue(any(item["问题"] == "预设口径已变更" for item in report["问题"]))

    def test_deprecated_preset_cannot_be_referenced(self) -> None:
        _, missing, message = self.profiles.create_entry(
            {"画像编号": "SEGM-T009", "分群名称": "x", "筛选规则": None, "预设编号": "FILT-0004"}
        )
        self.assertIn("已废弃", message)


class AuditTests(ChainTestCase):
    def test_seed_coverage_residue_is_cleared_at_bootstrap(self) -> None:
        report = profilechain.consistency_report()
        self.assertTrue(report["一致"], report["问题"])
        self.assertNotEqual(self.profile_row(3)["覆盖对象数"], 888)

    def test_audit_detects_stale_consumer(self) -> None:
        segment = store.find_by(profilechain.PROFILE_MODULE, "画像编号", "SEGM-0001")
        updated, message = self.profiles.update_entry(
            int(segment["id"]), {"画像维度": "城市,会员等级,活跃度"}
        )
        self.assertIsNotNone(updated, message)
        self.assertEqual(updated["快照版本"], 2)
        for action in ("提交画像评审", "发布画像"):
            _, message = self.profiles.run_action(int(segment["id"]), action)
            self.assertNotIn("不允许", message)

        report = profilechain.consistency_report()
        self.assertFalse(report["一致"])
        self.assertTrue(any(item["问题"] == "消费方版本滞后" for item in report["问题"]))

        synced, message = self.events.sync_snapshot(1)
        self.assertIsNotNone(synced, message)
        self.assertEqual(synced["快照版本"], 2)
        report = profilechain.consistency_report()
        self.assertTrue(report["一致"], report["问题"])

    def test_audit_detects_coverage_drift(self) -> None:
        self.profile_row(1)["覆盖对象数"] = 1
        report = profilechain.consistency_report()
        problems = {item["问题"] for item in report["问题"]}
        self.assertIn("覆盖数残留", problems)


if __name__ == "__main__":
    unittest.main()
