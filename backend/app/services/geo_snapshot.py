"""分析快照领域服务：对聚合分析结果创建版本化快照，记录分析口径、时间范围和结果条数。

区域快照发布：以边界版本和实际贡献网格为依据维护子区统计的发布状态，
支持单个子区撤回后重新确认；市级交接完成时冻结原贡献网格，
已发出的市级报告可按原贡献网格复算，查得当时的区域明细。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class GeoSnapshotService(BaseModuleService):
    MODULE = "geo_snapshot"
    ENTITY = "版本快照"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = ["快照编号", "分析口径", "网格层级", "时间范围", "结果条数", "生成方式", "快照水位", "生成时间"]
    REQUIRED_FIELDS = ["快照编号", "分析口径", "网格层级"]
    STATUS_ORDER = ["草稿", "待确认", "已发布", "已归档"]
    ACTION_RULES = {"提交确认": "待确认", "发布快照": "已发布", "标记归档": "已归档"}
    NEGATIVE_ACTIONS = []

    # 区域快照发布状态：子区统计按边界版本+实际贡献网格发布后的流转状态
    REGION_STATUS_ORDER = ["待确认", "已确认", "已交接", "已撤回"]
    # 生效中的状态：参与当前全市汇总、可登记进新的市级交接
    ACTIVE_REGION_STATUS = ("已确认", "已交接")
    # 市级交接状态
    HANDOVER_STATUS_ORDER = ["准备中", "已交接"]

    def create_snapshot(self, batch_key: str, analysis_scope: str = "full",
                         grid_level: str = "L6", time_range: str = "24h") -> dict[str, Any]:
        """读取热力聚合和OD矩阵数据集，创建分析口径版本快照，生成快照聚合数据集文件。"""
        # 读取热力聚合数据
        heatmap_data = []
        heatmap_file = self._data_dir("heatmap") / f"{batch_key}_heatmap.jsonl"
        if heatmap_file.exists():
            heatmap_data = self._read_jsonl(heatmap_file)

        # 读取OD矩阵数据
        od_data = []
        od_file = self._data_dir("od_matrix") / f"{batch_key}_od.jsonl"
        if od_file.exists():
            od_data = self._read_jsonl(od_file)

        # 读取区域聚合
        region_data = []
        region_file = self._data_dir("region_aggregates") / f"{batch_key}_region.jsonl"
        if region_file.exists():
            region_data = self._read_jsonl(region_file)

        total_results = len(heatmap_data) + len(od_data) + len(region_data)

        snapshot = {
            "snapshot_id": f"SNAP-{batch_key}-{int(time.time())}",
            "batch_key": batch_key,
            "analysis_scope": analysis_scope,
            "grid_level": grid_level,
            "time_range": time_range,
            "heatmap_cells": len(heatmap_data),
            "od_pairs": len(od_data),
            "region_aggregates": len(region_data),
            "total_results": total_results,
            "peak_heatmap_grid": max((h["point_count"] for h in heatmap_data), default=0),
            "total_trips": sum(od.get("trip_count", 0) for od in od_data),
            "generated_at": time.time(),
            "version": "v1.0",
        }

        output_file = self._data_dir("snapshots") / f"{batch_key}_snapshot.jsonl"
        written = self._write_jsonl(output_file, [snapshot])

        # 创建快照记录
        entry, _ = self.create_entry({
            "快照编号": snapshot["snapshot_id"],
            "分析口径": analysis_scope,
            "网格层级": grid_level,
            "时间范围": time_range,
            "结果条数": str(total_results),
            "生成方式": "auto_aggregate",
            "快照水位": f"热力{len(heatmap_data)}_OD{len(od_data)}_区域{len(region_data)}",
            "生成时间": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time())),
        })
        if entry:
            self.run_action(entry["id"], "发布快照")

        return {
            "batch_key": batch_key,
            "snapshot_id": snapshot["snapshot_id"],
            "total_results": total_results,
            "heatmap_cells": len(heatmap_data),
            "od_pairs": len(od_data),
            "output_file": str(output_file),
        }

    def get_snapshot(self, batch_key: str) -> dict[str, Any]:
        """读取批次的分析快照数据集。"""
        file_path = self._data_dir("snapshots") / f"{batch_key}_snapshot.jsonl"
        snapshots = self._read_jsonl(file_path)
        return snapshots[0] if snapshots else {}

    # -- 区域快照发布：子区统计的确认、撤回与重新确认 --

    def _load_board(self, snapshot_key: str) -> dict[str, Any]:
        """读取区域发布看板状态，不存在时初始化空看板。"""
        board = self._read_checkpoint(f"region_pub_{snapshot_key}")
        if board is None:
            board = {"snapshot_key": snapshot_key, "seq": 0, "contributions": [], "handovers": []}
        board.setdefault("seq", 0)
        board.setdefault("contributions", [])
        board.setdefault("handovers", [])
        return board

    def _save_board(self, board: dict[str, Any]) -> None:
        self._write_checkpoint(f"region_pub_{board['snapshot_key']}", board)

    @staticmethod
    def _find_contribution(board: dict[str, Any], region_key: str,
                           version_id: str | None = None,
                           statuses: tuple[str, ...] | None = None) -> dict[str, Any] | None:
        candidates = [c for c in board["contributions"] if c["region_key"] == region_key]
        if version_id:
            candidates = [c for c in candidates if c["version_id"] == version_id]
        if statuses:
            candidates = [c for c in candidates if c["status"] in statuses]
        if not candidates:
            return None
        return max(candidates, key=lambda c: c.get("published_at", 0))

    @staticmethod
    def _find_handover(board: dict[str, Any], handover_id: str | None = None,
                       status: str | None = None) -> dict[str, Any] | None:
        candidates = list(board["handovers"])
        if handover_id:
            candidates = [h for h in candidates if h["handover_id"] == handover_id]
        if status:
            candidates = [h for h in candidates if h["status"] == status]
        if not candidates:
            return None
        return max(candidates, key=lambda h: h.get("prepared_at", 0))

    @staticmethod
    def _register_into_open_handovers(board: dict[str, Any], version_id: str) -> list[str]:
        touched = []
        for ho in board["handovers"]:
            if ho["status"] == "准备中" and version_id not in ho["registered_version_ids"]:
                ho["registered_version_ids"].append(version_id)
                touched.append(ho["handover_id"])
        return touched

    @staticmethod
    def _deregister_from_open_handovers(board: dict[str, Any], version_id: str) -> None:
        for ho in board["handovers"]:
            if ho["status"] == "准备中" and version_id in ho["registered_version_ids"]:
                ho["registered_version_ids"].remove(version_id)

    def publish_region_stat(self, snapshot_key: str, region_key: str, boundary_version: str,
                            grid_cells: list[dict[str, Any]], note: str = "") -> dict[str, Any]:
        """子区按边界版本发布统计数据，记录实际贡献网格，初始状态为待确认。"""
        if not str(region_key or "").strip():
            return {"error": "子区标识不能为空"}
        if not str(boundary_version or "").strip():
            return {"error": "边界版本不能为空"}
        cells = []
        for cell in grid_cells or []:
            grid_id = str(cell.get("grid_id", "")).strip()
            try:
                points = int(cell.get("point_count", 0))
            except (TypeError, ValueError):
                return {"error": f"贡献网格 {grid_id or '?'} 的点数不是有效数字"}
            if not grid_id or points <= 0:
                return {"error": "实际贡献网格必须包含网格编号且点数大于0"}
            cells.append({"grid_id": grid_id, "point_count": points})
        if not cells:
            return {"error": "实际贡献网格不能为空"}

        board = self._load_board(snapshot_key)
        board["seq"] += 1
        contribution = {
            "version_id": f"RSV-{board['seq']:04d}",
            "snapshot_key": snapshot_key,
            "region_key": region_key,
            "boundary_version": boundary_version,
            "grid_cells": cells,
            "point_count": sum(c["point_count"] for c in cells),
            "status": "待确认",
            "note": note,
            "retract_reason": "",
            "published_at": time.time(),
            "confirmed_at": None,
            "retracted_at": None,
        }
        board["contributions"].append(contribution)
        self._save_board(board)
        return dict(contribution)

    def confirm_region_stat(self, snapshot_key: str, region_key: str,
                            version_id: str | None = None) -> dict[str, Any]:
        """确认子区统计：待确认→已确认。同区其他生效版本与之互不相容，自动撤回，
        保证区域汇总不会合并两个互不相容的子区版本。"""
        board = self._load_board(snapshot_key)
        target = self._find_contribution(board, region_key, version_id, statuses=("待确认",))
        if target is None:
            return {"error": f"子区 {region_key} 没有待确认的统计版本"}
        now = time.time()
        superseded = []
        for other in board["contributions"]:
            if other is target or other["region_key"] != region_key:
                continue
            if other["status"] in self.ACTIVE_REGION_STATUS:
                other["status"] = "已撤回"
                other["retracted_at"] = now
                other["retract_reason"] = f"被新版本 {target['version_id']} 取代（边界版本互不相容，不得合并）"
                superseded.append(other["version_id"])
                self._deregister_from_open_handovers(board, other["version_id"])
        target["status"] = "已确认"
        target["confirmed_at"] = now
        registered = self._register_into_open_handovers(board, target["version_id"])
        self._save_board(board)
        result = dict(target)
        result["superseded_versions"] = superseded
        result["registered_handovers"] = registered
        return result

    def retract_region_stat(self, snapshot_key: str, region_key: str,
                            version_id: str | None = None, reason: str = "边界更正") -> dict[str, Any]:
        """撤回单个子区统计：已确认/已交接→已撤回。撤回后旧版不再参与新的交接与全市汇总；
        已完成交接里冻结的原贡献网格不受影响，仍可复盘。"""
        board = self._load_board(snapshot_key)
        if version_id:
            target = self._find_contribution(board, region_key, version_id)
            if target is None:
                return {"error": f"子区 {region_key} 的版本 {version_id} 不存在"}
            if target["status"] not in self.ACTIVE_REGION_STATUS:
                return {"error": f"子区 {region_key} 的版本 {version_id} 当前为{target['status']}，不能撤回"}
        else:
            target = self._find_contribution(board, region_key, statuses=self.ACTIVE_REGION_STATUS)
            if target is None:
                return {"error": f"子区 {region_key} 没有已确认或已交接的统计版本可撤回"}
        target["status"] = "已撤回"
        target["retracted_at"] = time.time()
        target["retract_reason"] = reason
        self._deregister_from_open_handovers(board, target["version_id"])
        self._save_board(board)
        return dict(target)

    # -- 市级交接：登记、完成与复盘 --

    def prepare_handover(self, snapshot_key: str, shift_label: str = "") -> dict[str, Any]:
        """开启一份市级交接准备：登记当前全部生效的子区统计版本。
        准备期间子区的确认与撤回会同步登记变化，交接完成前都可调整。"""
        board = self._load_board(snapshot_key)
        for ho in board["handovers"]:
            if ho["status"] == "准备中":
                return {"error": f"交接 {ho['handover_id']} 仍在准备中，不能重复开启"}
        board["seq"] += 1
        handover = {
            "handover_id": f"HO-{snapshot_key}-{board['seq']:04d}",
            "snapshot_key": snapshot_key,
            "shift_label": shift_label,
            "status": "准备中",
            "registered_version_ids": [
                c["version_id"] for c in board["contributions"]
                if c["status"] in self.ACTIVE_REGION_STATUS
            ],
            "frozen_regions": [],
            "total_points": 0,
            "prepared_at": time.time(),
            "completed_at": None,
        }
        board["handovers"].append(handover)
        self._save_board(board)
        return dict(handover)

    def complete_handover(self, snapshot_key: str, handover_id: str | None = None,
                          grid_level: str = "L6", time_range: str = "24h") -> dict[str, Any]:
        """完成市级交接：冻结登记的子区统计（边界版本+实际贡献网格），生成本班市级报告。
        交接完成后内容不可再改，已发出的报告可按原贡献网格复算复盘。"""
        board = self._load_board(snapshot_key)
        handover = self._find_handover(board, handover_id, status="准备中")
        if handover is None:
            return {"error": "没有准备中的市级交接可以完成"}
        by_id = {c["version_id"]: c for c in board["contributions"]}
        frozen = []
        now = time.time()
        for vid in handover["registered_version_ids"]:
            contrib = by_id.get(vid)
            if contrib is None or contrib["status"] not in self.ACTIVE_REGION_STATUS:
                continue
            frozen.append({
                "version_id": contrib["version_id"],
                "region_key": contrib["region_key"],
                "boundary_version": contrib["boundary_version"],
                "grid_cells": [dict(cell) for cell in contrib["grid_cells"]],
                "point_count": contrib["point_count"],
            })
            contrib["status"] = "已交接"
        handover["frozen_regions"] = frozen
        handover["total_points"] = sum(r["point_count"] for r in frozen)
        handover["status"] = "已交接"
        handover["completed_at"] = now

        # 登记版本快照条目并发布，作为本班市级报告
        watermark = "；".join(
            f"{r['region_key']}@{r['boundary_version']}={r['point_count']}" for r in frozen
        ) or "无子区"
        entry, _ = self.create_entry({
            "快照编号": handover["handover_id"],
            "分析口径": "区域交接",
            "网格层级": grid_level,
            "时间范围": handover.get("shift_label") or time_range,
            "结果条数": str(handover["total_points"]),
            "生成方式": "region_handover",
            "快照水位": watermark,
            "生成时间": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        })
        if entry:
            self.run_action(entry["id"], "提交确认")
            self.run_action(entry["id"], "发布快照")
            handover["snapshot_entry_id"] = entry["id"]
        self._save_board(board)

        # 交接审计数据集落盘：已发出的报告供复盘入口读取
        audit_file = self._data_dir("snapshots") / f"{snapshot_key}_handovers.jsonl"
        self._write_jsonl(audit_file, [handover])
        return dict(handover)

    def review_handover(self, snapshot_key: str, handover_id: str | None = None) -> dict[str, Any]:
        """复盘已交接的市级报告：按冻结的原贡献网格复算各子区点数与总量，
        查得交接当时的区域明细。"""
        board = self._load_board(snapshot_key)
        handover = self._find_handover(board, handover_id)
        if handover is None:
            return {"error": "交接记录不存在"}
        if handover["status"] != "已交接":
            return {"error": f"交接 {handover['handover_id']} 尚未完成，不能复盘"}
        regions = []
        consistent = True
        for region in handover["frozen_regions"]:
            recomputed = sum(cell["point_count"] for cell in region["grid_cells"])
            regions.append({
                "region_key": region["region_key"],
                "boundary_version": region["boundary_version"],
                "grid_cells": region["grid_cells"],
                "point_count": region["point_count"],
                "recomputed_points": recomputed,
            })
            if recomputed != region["point_count"]:
                consistent = False
        recomputed_total = sum(r["recomputed_points"] for r in regions)
        if recomputed_total != handover["total_points"]:
            consistent = False
        return {
            "handover_id": handover["handover_id"],
            "snapshot_key": snapshot_key,
            "shift_label": handover.get("shift_label", ""),
            "status": handover["status"],
            "regions": regions,
            "total_points": handover["total_points"],
            "recomputed_total": recomputed_total,
            "consistent": consistent,
            "completed_at": handover.get("completed_at"),
            "reviewed_at": time.time(),
        }

    # -- 全市汇总与发布状态看板 --

    def current_aggregate(self, snapshot_key: str) -> dict[str, Any]:
        """当前全市统计：每个子区只取最新生效版本；已撤回的旧版不参与，
        互不相容的两个子区版本不得合并进同一汇总。"""
        board = self._load_board(snapshot_key)
        by_region: dict[str, dict[str, Any]] = {}
        for contrib in board["contributions"]:
            if contrib["status"] not in self.ACTIVE_REGION_STATUS:
                continue
            existing = by_region.get(contrib["region_key"])
            if existing is None or (contrib.get("confirmed_at") or 0) >= (existing.get("confirmed_at") or 0):
                by_region[contrib["region_key"]] = contrib
        regions = [{
            "region_key": c["region_key"],
            "version_id": c["version_id"],
            "boundary_version": c["boundary_version"],
            "point_count": c["point_count"],
            "status": c["status"],
        } for c in by_region.values()]
        return {
            "snapshot_key": snapshot_key,
            "regions": regions,
            "total_points": sum(r["point_count"] for r in regions),
        }

    def region_board(self, snapshot_key: str) -> dict[str, Any]:
        """区域快照发布状态看板：各子区版本状态、交接记录与当前全市汇总。"""
        board = self._load_board(snapshot_key)
        return {
            "snapshot_key": snapshot_key,
            "contributions": [dict(c) for c in board["contributions"]],
            "handovers": [dict(h) for h in board["handovers"]],
            "current_aggregate": self.current_aggregate(snapshot_key),
        }