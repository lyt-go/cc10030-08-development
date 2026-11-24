"""区域快照发布与市级交接：子区统计确认、撤回、重新确认与已交接报告复盘。

场景主线：甲区 4 点、乙区 6 点依次确认并完成本班市级交接（10 点）；
准备下一班报告时甲区边界更正撤回原统计，乙区 6 点随当前交接登记；
甲区按更正后的边界版本重新确认后回到全市统计；
已交接的 10 点按原贡献网格复算查回。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def setup_data_dir(monkeypatch):
    """每个测试用例使用独立临时目录写入数据产物文件。"""
    tmpdir = Path(tempfile.mkdtemp(prefix="geo_region_handover_"))
    import app.services.base as base_mod
    original_root = base_mod.DATA_ROOT
    base_mod.DATA_ROOT = tmpdir
    for sub in ["snapshots", "checkpoints"]:
        (tmpdir / sub).mkdir(parents=True, exist_ok=True)
    yield tmpdir
    base_mod.DATA_ROOT = original_root
    if tmpdir.exists():
        shutil.rmtree(tmpdir, ignore_errors=True)


def _publish_and_confirm(svc, key, region, boundary, cells):
    """子区发布统计并确认，返回确认后的版本记录。"""
    published = svc.publish_region_stat(key, region, boundary, cells)
    assert "error" not in published
    confirmed = svc.confirm_region_stat(key, region)
    assert confirmed.get("status") == "已确认"
    return confirmed


class TestRegionHandoverFlow:
    """按班次推进的完整交接流程：确认→交接→撤回→重新确认→复盘。"""

    def test_retract_reconfirm_and_review_flow(self, setup_data_dir):
        from app.services.geo_snapshot import GeoSnapshotService
        svc = GeoSnapshotService()
        key = "CITY-SHIFT-001"

        # 依次确认甲乙两区：甲区 4 点（边界 BV-A1），乙区 6 点（边界 BV-B1）
        jia_v1 = _publish_and_confirm(svc, key, "甲区", "BV-A1", [
            {"grid_id": "G-A1", "point_count": 3},
            {"grid_id": "G-A2", "point_count": 1},
        ])
        assert jia_v1["point_count"] == 4
        yi_v1 = _publish_and_confirm(svc, key, "乙区", "BV-B1", [
            {"grid_id": "G-B1", "point_count": 6},
        ])
        assert yi_v1["point_count"] == 6

        # 本班市级交接完成：10 点报告发出，值班人员据此安排本班巡查
        h1 = svc.prepare_handover(key, shift_label="本班")
        assert "error" not in h1
        assert set(h1["registered_version_ids"]) == {jia_v1["version_id"], yi_v1["version_id"]}
        done1 = svc.complete_handover(key)
        assert done1["status"] == "已交接"
        assert done1["total_points"] == 10
        assert {r["region_key"] for r in done1["frozen_regions"]} == {"甲区", "乙区"}

        # 准备下一班报告：甲区边界更正，在交接准备完成前撤回原统计
        h2 = svc.prepare_handover(key, shift_label="下一班")
        assert "error" not in h2
        retracted = svc.retract_region_stat(key, "甲区", reason="边界更正")
        assert retracted.get("status") == "已撤回"
        assert retracted["version_id"] == jia_v1["version_id"]

        # 核对1：乙区 6 点仍随当前市级交接登记，甲区旧版不再参与新的交接
        board = svc.region_board(key)
        open_h2 = next(h for h in board["handovers"] if h["handover_id"] == h2["handover_id"])
        assert yi_v1["version_id"] in open_h2["registered_version_ids"]
        assert jia_v1["version_id"] not in open_h2["registered_version_ids"]

        # 从这一份交接继续安排下一班巡查：只含乙区 6 点
        done2 = svc.complete_handover(key)
        assert done2["status"] == "已交接"
        assert done2["total_points"] == 6
        assert [r["region_key"] for r in done2["frozen_regions"]] == ["乙区"]

        # 撤回后甲区不在全市统计中
        agg = svc.current_aggregate(key)
        assert agg["total_points"] == 6
        assert [r["region_key"] for r in agg["regions"]] == ["乙区"]

        # 核对2：甲区按更正后的边界版本重新发布并确认后，才回到全市统计
        jia_v2 = _publish_and_confirm(svc, key, "甲区", "BV-A2", [
            {"grid_id": "G-A3", "point_count": 5},
        ])
        agg2 = svc.current_aggregate(key)
        assert agg2["total_points"] == 11
        jia_current = [r for r in agg2["regions"] if r["region_key"] == "甲区"]
        assert len(jia_current) == 1  # 区域汇总不合并两个互不相容的子区版本
        assert jia_current[0]["boundary_version"] == "BV-A2"
        assert jia_current[0]["point_count"] == 5
        assert jia_current[0]["version_id"] == jia_v2["version_id"]

        # 旧版保持已撤回，不再参与任何新交接
        board2 = svc.region_board(key)
        jia_old = next(c for c in board2["contributions"]
                       if c["version_id"] == jia_v1["version_id"])
        assert jia_old["status"] == "已撤回"

        # 核对3：已交接的 10 点按原贡献网格复算，查得当时的区域明细
        review1 = svc.review_handover(key, h1["handover_id"])
        assert review1["consistent"] is True
        assert review1["total_points"] == 10
        assert review1["recomputed_total"] == 10
        detail = {r["region_key"]: r for r in review1["regions"]}
        assert detail["甲区"]["boundary_version"] == "BV-A1"
        assert detail["甲区"]["recomputed_points"] == 4
        assert detail["甲区"]["grid_cells"] == [
            {"grid_id": "G-A1", "point_count": 3},
            {"grid_id": "G-A2", "point_count": 1},
        ]
        assert detail["乙区"]["recomputed_points"] == 6

        # 下一班交接同样可复盘
        review2 = svc.review_handover(key, h2["handover_id"])
        assert review2["consistent"] is True
        assert review2["recomputed_total"] == 6
        assert [r["region_key"] for r in review2["regions"]] == ["乙区"]

    def test_handover_registration_tracks_confirm_and_retract(self, setup_data_dir):
        """交接准备期间：子区确认自动登记进交接，撤回自动移出。"""
        from app.services.geo_snapshot import GeoSnapshotService
        svc = GeoSnapshotService()
        key = "CITY-SHIFT-002"

        # 先开准备，再确认子区：确认时自动登记
        h = svc.prepare_handover(key, shift_label="准备中班次")
        assert h["registered_version_ids"] == []
        confirmed = _publish_and_confirm(svc, key, "乙区", "BV-B1", [
            {"grid_id": "G-B1", "point_count": 6},
        ])
        assert confirmed["registered_handovers"] == [h["handover_id"]]

        # 准备完成前撤回：自动移出登记
        svc.retract_region_stat(key, "乙区", reason="边界更正")
        board = svc.region_board(key)
        open_h = next(ho for ho in board["handovers"] if ho["handover_id"] == h["handover_id"])
        assert open_h["registered_version_ids"] == []

        # 重新确认后再次登记，交接只含最新版本
        _publish_and_confirm(svc, key, "乙区", "BV-B2", [
            {"grid_id": "G-B2", "point_count": 7},
        ])
        done = svc.complete_handover(key)
        assert done["total_points"] == 7
        assert [r["boundary_version"] for r in done["frozen_regions"]] == ["BV-B2"]


class TestRegionVersionGuards:
    """版本互不相容与状态流转约束。"""

    def test_reconfirm_supersedes_incompatible_boundary(self, setup_data_dir):
        """同区以不同边界版本重新确认时旧版自动撤回，区域汇总不合并两个版本。"""
        from app.services.geo_snapshot import GeoSnapshotService
        svc = GeoSnapshotService()
        key = "CITY-GUARD-001"

        v1 = _publish_and_confirm(svc, key, "甲区", "BV-A1", [
            {"grid_id": "G-A1", "point_count": 4},
        ])
        v2 = _publish_and_confirm(svc, key, "甲区", "BV-A2", [
            {"grid_id": "G-A2", "point_count": 5},
        ])
        assert v1["version_id"] in v2["superseded_versions"]

        agg = svc.current_aggregate(key)
        assert agg["total_points"] == 5  # 不是 4+5
        assert [r["version_id"] for r in agg["regions"]] == [v2["version_id"]]

        board = svc.region_board(key)
        old = next(c for c in board["contributions"] if c["version_id"] == v1["version_id"])
        assert old["status"] == "已撤回"
        assert "互不相容" in old["retract_reason"]

    def test_retract_requires_active_version(self, setup_data_dir):
        """未确认或已撤回的版本不能撤回。"""
        from app.services.geo_snapshot import GeoSnapshotService
        svc = GeoSnapshotService()
        key = "CITY-GUARD-002"

        # 未发布过：不能撤回
        missing = svc.retract_region_stat(key, "甲区")
        assert "error" in missing

        # 仅发布未确认：不能撤回
        svc.publish_region_stat(key, "甲区", "BV-A1", [
            {"grid_id": "G-A1", "point_count": 4},
        ])
        pending = svc.retract_region_stat(key, "甲区")
        assert "error" in pending

        # 确认后可撤回；已撤回不能重复撤回
        svc.confirm_region_stat(key, "甲区")
        retracted = svc.retract_region_stat(key, "甲区")
        assert retracted.get("status") == "已撤回"
        again = svc.retract_region_stat(key, "甲区")
        assert "error" in again

    def test_publish_requires_boundary_and_grid(self, setup_data_dir):
        """发布子区统计必须带边界版本和实际贡献网格。"""
        from app.services.geo_snapshot import GeoSnapshotService
        svc = GeoSnapshotService()
        key = "CITY-GUARD-003"

        assert "error" in svc.publish_region_stat(key, "甲区", "", [
            {"grid_id": "G-A1", "point_count": 4},
        ])
        assert "error" in svc.publish_region_stat(key, "甲区", "BV-A1", [])
        assert "error" in svc.publish_region_stat(key, "甲区", "BV-A1", [
            {"grid_id": "", "point_count": 4},
        ])
        assert "error" in svc.publish_region_stat(key, "甲区", "BV-A1", [
            {"grid_id": "G-A1", "point_count": 0},
        ])

    def test_review_requires_completed_handover(self, setup_data_dir):
        """准备中的交接不能复盘；不存在的交接返回错误。"""
        from app.services.geo_snapshot import GeoSnapshotService
        svc = GeoSnapshotService()
        key = "CITY-GUARD-004"

        _publish_and_confirm(svc, key, "乙区", "BV-B1", [
            {"grid_id": "G-B1", "point_count": 6},
        ])
        h = svc.prepare_handover(key)
        reviewing = svc.review_handover(key, h["handover_id"])
        assert "error" in reviewing
        assert "error" in svc.review_handover(key, "HO-9999")

    def test_handover_prepare_not_reentrant(self, setup_data_dir):
        """已有准备中的交接时不能重复开启。"""
        from app.services.geo_snapshot import GeoSnapshotService
        svc = GeoSnapshotService()
        key = "CITY-GUARD-005"

        first = svc.prepare_handover(key)
        assert "error" not in first
        second = svc.prepare_handover(key)
        assert "error" in second

    def test_completed_handover_publishes_snapshot_entry(self, setup_data_dir):
        """完成交接生成已发布的版本快照条目，结果条数与交接总量一致。"""
        from app.services.geo_snapshot import GeoSnapshotService
        from app.store import store
        svc = GeoSnapshotService()
        key = "CITY-GUARD-006"

        _publish_and_confirm(svc, key, "甲区", "BV-A1", [
            {"grid_id": "G-A1", "point_count": 4},
        ])
        _publish_and_confirm(svc, key, "乙区", "BV-B1", [
            {"grid_id": "G-B1", "point_count": 6},
        ])
        h = svc.prepare_handover(key, shift_label="本班")
        done = svc.complete_handover(key)

        entry = store.find("geo_snapshot", done["snapshot_entry_id"])
        assert entry is not None
        assert entry["快照编号"] == h["handover_id"]
        assert entry["status"] == "已发布"
        assert entry["结果条数"] == "10"
        assert entry["分析口径"] == "区域交接"
        assert "甲区@BV-A1=4" in entry["快照水位"]
        assert "乙区@BV-B1=6" in entry["快照水位"]
        assert done["snapshot_entry_id"] == entry["id"]

    def test_completed_handover_audit_dataset_written(self, setup_data_dir):
        """完成交接后审计数据集落盘，复盘结果与落盘记录一致。"""
        from app.services.geo_snapshot import GeoSnapshotService
        svc = GeoSnapshotService()
        key = "CITY-GUARD-007"

        _publish_and_confirm(svc, key, "乙区", "BV-B1", [
            {"grid_id": "G-B1", "point_count": 6},
        ])
        h = svc.prepare_handover(key)
        svc.complete_handover(key)

        audit_file = svc._data_dir("snapshots") / f"{key}_handovers.jsonl"
        records = svc._read_jsonl(audit_file)
        assert len(records) == 1
        assert records[0]["handover_id"] == h["handover_id"]
        assert records[0]["total_points"] == 6

        review = svc.review_handover(key, h["handover_id"])
        assert review["recomputed_total"] == records[0]["total_points"]
