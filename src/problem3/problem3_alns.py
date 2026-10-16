"""Problem 3 联合 ALNS Phase A：解结构、固定尺度与快速中继解码器。

本阶段只建立能够对运输高层解进行完整解码的基础设施，并验证问题三
baseline warm start。正式 ALNS 的 destroy/repair/local-search 与主循环留到
Phase B。运输模型、资源调度和目标 Z1 均直接复用问题二正式实现。
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path
from time import perf_counter
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
import argparse
import math
import random
import sys

import numpy as np
import pandas as pd


# Preserve historical module names used by trusted communication caches.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
import project_paths as paths
paths.bootstrap()
MODULE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = paths.ROOT

import problem2_alns as p2_core  # noqa: E402
import problem2_alns_improved as p2_improved  # noqa: E402
import problem3_baseline as baseline  # noqa: E402
import problem3_communication as communication  # noqa: E402
import problem3_core as core  # noqa: E402
import problem3_relay as relay  # noqa: E402
import problem3_segment_communication_cache as segment_cache  # noqa: E402


PROBLEM2_RESULT_FILE = paths.input_file("problem2", "problem2_Z1_priority_result.xlsx")
STEP4_FILE = paths.input_file("problem3", "problem3_relay_coverage.xlsx")
BASELINE_FILE = paths.input_file("problem3", "problem3_baseline.xlsx")
ELITE_POOL_FILE = paths.input_file("problem3", "problem3_relay_elite_pool.xlsx")
ELITE_POOL_OUTPUT_FILE = paths.output_file("problem3", "problem3_relay_elite_pool.xlsx")

# Problem 3 正式固定参考尺度。任何 evaluation 均不得动态修改。
PROBLEM3_NORMALIZATION_SCALES: Tuple[float, float, float, float] = (
    6_321_600.0,
    6_985.307588,
    68.808141175,
    23.0,
)
FORMAL_WARM_OBJECTIVES: Tuple[float, float, float, float] = (
    0.0,
    6_985.307588484,
    68.808128466,
    23.0,
)
FORMAL_WARM_NORMALIZED: Tuple[float, float, float, float] = tuple(
    value / scale
    for value, scale in zip(FORMAL_WARM_OBJECTIVES, PROBLEM3_NORMALIZATION_SCALES)
)  # type: ignore[assignment]
FORMAL_WARM_F = sum(0.25 * value for value in FORMAL_WARM_NORMALIZED)

BASELINE_CANDIDATE_IDS: Tuple[str, str, str] = (
    "L2-P0000192",
    "L1-P0079429",
    "L1-P0057577",
)
BASELINE_POSITION_IDS: Mapping[str, str] = {
    "L2-P0000192": "P1",
    "L1-P0079429": "P2",
    "L1-P0057577": "P3",
}

ELITE_PRESELECTION_SIZE = 300
ELITE_TARGET_SIZE = 240
ELITE_COVERAGE_SEED_COUNT = 60
ELITE_RARE_SEED_COUNT = 40
ELITE_LOW_ENERGY_SEED_COUNT = 20
ELITE_PER_HEIGHT_SEED_COUNT = 6
DEFAULT_COVERAGE_WORKERS = relay.DEFAULT_WORKER_COUNT
NUMERIC_TOLERANCE = 1e-9
# 仅用于LEVEL 1目标优劣/相等分类；不参与目标计算、约束或SA接受概率。
# 该值高于0.01 s状态切换误差对应的综合目标扰动（约1e-7）。
OBJECTIVE_COMPARISON_TOLERANCE = 1e-6


@dataclass
class Problem3Solution:
    """第一版搜索解；transport_sorties 的顺序就是显式优先序 sigma。"""

    transport_sorties: Tuple[p2_core.Sortie, ...]
    decoded_state: Optional["Problem3DecodedState"] = field(
        default=None, compare=False, repr=False
    )

    @property
    def transport_priority_sigma(self) -> Tuple[int, ...]:
        return tuple(range(1, len(self.transport_sorties) + 1))

    def copy_genes(self) -> "Problem3Solution":
        return Problem3Solution(tuple(self.transport_sorties))


@dataclass(frozen=True)
class DirectOutageProfile:
    """与绝对开始时刻无关的单架次 UG 中断剖面。"""

    relative_start_s: float
    relative_end_s: float
    start_phase: str
    end_phase: str


@dataclass
class ElitePool:
    candidates: Tuple[relay.RelayCandidate, ...]
    fallback_rg_usable: pd.DataFrame
    baseline_states: relay.SpatialDemandStates
    selection_table: pd.DataFrame
    static_cache_table: pd.DataFrame
    preprocessing_runtime_s: float


@dataclass
class Problem3DecodedState:
    transport_evaluation: p2_core.Evaluation
    trajectories: Tuple[core.TransportTrajectory, ...]
    outages: Tuple[core.OutageInterval, ...]
    demands: Tuple[core.CommunicationDemand, ...]
    relay_positions: Tuple[baseline.RelayPosition, ...]
    relay_service_tasks: Tuple[baseline.RelayServiceTask, ...]
    relay_sorties: Tuple[baseline.ScheduledRelaySortie, ...]
    communication_check: baseline.CommunicationCheckResult
    feasibility: pd.DataFrame
    objectives: Tuple[float, float, float, float]
    normalized: Tuple[float, float, float, float]
    objective: float
    feasible: bool
    relay_decode_runtime_s: float
    full_verification_runtime_s: float
    selected_candidate_ids: Tuple[str, ...]


@dataclass(frozen=True)
class Problem3EvaluationResult:
    """供后续 ALNS 使用的无异常评价结果。"""

    decoded_state: Optional[Problem3DecodedState]
    feasible: bool
    objective: float
    infeasibility_reason: str = ""


@dataclass(frozen=True)
class PhaseAContext:
    transport_data: p2_core.ProblemData
    nodes: Mapping[str, core.Node3D]
    cruise_altitude: pd.DataFrame
    communication_parameters: communication.CommunicationParameters
    gateway: communication.GatewayEndpoint
    dem: communication.DemData
    relay_parameters: baseline.RelayParameters


EVALUATION_STAGE_LABELS: Mapping[str, str] = {
    "T1": "transport feasibility / metrics",
    "T2": "transport resource scheduling",
    "T3": "trajectory construction",
    "T4": "UG outage reconstruction",
    "T5": "UR coverage/cache lookup",
    "T6": "relay candidate selection",
    "T7": "SERVICE merge/split",
    "T8": "R01/R02 scheduling",
    "T9": "relay component scheduling",
    "T10": "objective calculation",
    "T11": "other",
}

EVALUATOR_CALLERS: Tuple[str, ...] = (
    "MAIN_CANDIDATE",
    "GREEDY_REPAIR",
    "REGRET2_REPAIR",
    "REGRET3_REPAIR",
    "DEADLINE_REPAIR",
    "COMMUNICATION_AWARE_REPAIR",
    "LOCAL_RELOCATE",
    "LOCAL_SWAP",
    "LOCAL_2OPT",
    "LOCAL_UAV_TYPE",
    "SIGMA_LOCAL",
    "OTHER",
)


@dataclass
class EvaluationLayerProfiler:
    """仅记录 LEVEL 1 cache-miss 内部阶段，不把 LEVEL 2 混入统计。"""

    samples_s: Dict[str, List[float]] = field(
        default_factory=lambda: {key: [] for key in EVALUATION_STAGE_LABELS}
    )

    def add(self, stage: str, elapsed_s: float) -> None:
        self.samples_s[stage].append(max(0.0, float(elapsed_s)))

    def table(self) -> pd.DataFrame:
        rows = []
        for stage, label in EVALUATION_STAGE_LABELS.items():
            values = np.asarray(self.samples_s[stage], dtype=float)
            rows.append(
                {
                    "Stage": stage,
                    "Description": label,
                    "Count": len(values),
                    "Total_s": float(values.sum()) if len(values) else 0.0,
                    "Mean_s": float(values.mean()) if len(values) else 0.0,
                    "Median_s": float(np.median(values)) if len(values) else 0.0,
                    "Max_s": float(values.max()) if len(values) else 0.0,
                }
            )
        return pd.DataFrame(rows)


@dataclass
class CallerProfile:
    calls: int = 0
    total_s: float = 0.0
    times_s: List[float] = field(default_factory=list)
    cache_hits: int = 0
    cache_misses: int = 0


@dataclass
class SearchPerformanceCounters:
    repaired_box_counts: List[int] = field(default_factory=list)
    insertion_candidates_per_box: List[int] = field(default_factory=list)
    repair_full_calls: List[int] = field(default_factory=list)
    local_trials: List[int] = field(default_factory=list)
    local_full_calls: List[int] = field(default_factory=list)
    local_elapsed_s: List[float] = field(default_factory=list)
    local_improvements: int = 0
    geometry_prefill_s: List[float] = field(default_factory=list)
    level0_score_times_s: List[float] = field(default_factory=list)

    def table(self) -> pd.DataFrame:
        def mean(values: Sequence[float]) -> float:
            return float(np.mean(values)) if values else 0.0

        return pd.DataFrame(
            [
                {"Metric": "Repair_Invocations", "Value": len(self.repaired_box_counts)},
                {"Metric": "Mean_Boxes_Per_Repair", "Value": mean(self.repaired_box_counts)},
                {"Metric": "Mean_Insertion_Candidates_Per_Box", "Value": mean(self.insertion_candidates_per_box)},
                {"Metric": "Mean_Level1_Calls_Per_Repair", "Value": mean(self.repair_full_calls)},
                {"Metric": "Local_Invocations", "Value": len(self.local_trials)},
                {"Metric": "Mean_Local_Trials", "Value": mean(self.local_trials)},
                {"Metric": "Mean_Level1_Calls_Per_Local", "Value": mean(self.local_full_calls)},
                {"Metric": "Local_Total_s", "Value": float(sum(self.local_elapsed_s))},
                {"Metric": "Local_Improvements", "Value": self.local_improvements},
                {"Metric": "TopK_Geometry_Prefill_Count", "Value": len(self.geometry_prefill_s)},
                {"Metric": "TopK_Geometry_Prefill_Total_s", "Value": float(sum(self.geometry_prefill_s))},
                {"Metric": "TopK_Geometry_Prefill_Mean_s", "Value": mean(self.geometry_prefill_s)},
                {"Metric": "Level0_Score_Count", "Value": len(self.level0_score_times_s)},
                {"Metric": "Level0_Score_Mean_ms", "Value": 1000.0 * mean(self.level0_score_times_s)},
                {"Metric": "Level0_Score_Max_ms", "Value": 1000.0 * max(self.level0_score_times_s, default=0.0)},
            ]
        )


def validate_objective_weights(
    weights: Sequence[float],
) -> Tuple[float, float, float, float]:
    if len(weights) != 4:
        raise ValueError("Problem 3 目标权重必须恰好包含4项")
    result = tuple(float(value) for value in weights)
    if any(value < 0.0 for value in result):
        raise ValueError("Problem 3 目标权重不能为负")
    if not math.isclose(sum(result), 1.0, abs_tol=1e-12):
        raise ValueError("Problem 3 目标权重之和必须为1")
    return result  # type: ignore[return-value]


def load_phase_a_context() -> PhaseAContext:
    transport_data = p2_core.load_data()
    _, cruise_altitude = core.load_segment_altitudes()
    communication_parameters = communication.load_communication_parameters()
    gateway = communication.load_g01_endpoint(communication_parameters)
    return PhaseAContext(
        transport_data=transport_data,
        nodes=core.load_nodes(),
        cruise_altitude=cruise_altitude,
        communication_parameters=communication_parameters,
        gateway=gateway,
        dem=communication.load_dem(),
        relay_parameters=baseline.load_relay_parameters(),
    )


def load_baseline_warm_start(
    path: Path = PROBLEM2_RESULT_FILE,
) -> Problem3Solution:
    """从正式结果恢复高层运输基因，不读取既有中继调度作为解码结果。"""
    table = pd.read_excel(paths.require_file(path), sheet_name="Sorties").sort_values("Schedule_Order")
    sorties = tuple(
        p2_core.Sortie(
            box_ids=tuple(str(row.Box_IDs).split(";")),
            route=core._parse_route(str(row.Route)),
            uav_type=str(row.UAV_Type),
        )
        for row in table.itertuples(index=False)
    )
    return Problem3Solution(sorties)


def decode_transport(
    solution: Problem3Solution,
    context: PhaseAContext,
    profiler: Optional[EvaluationLayerProfiler] = None,
) -> Tuple[p2_core.Evaluation, Tuple[core.TransportTrajectory, ...]]:
    """调用问题二正式 evaluator，再把其调度展开为问题三三维轨迹。"""
    feasibility_started = perf_counter()
    assigned = [
        box_id
        for sortie in solution.transport_sorties
        for box_id in sortie.box_ids
    ]
    counts = Counter(assigned)
    local_feasible = (
        set(assigned) == set(context.transport_data.boxes)
        and all(value == 1 for value in counts.values())
        and all(
            p2_core.check_sortie_feasibility(sortie, context.transport_data)
            for sortie in solution.transport_sorties
        )
    )
    if profiler is not None:
        profiler.add("T1", perf_counter() - feasibility_started)

    scheduling_started = perf_counter()
    evaluation = p2_improved.evaluate_solution(
        solution.transport_sorties,
        context.transport_data,
        require_complete=True,
    )
    if profiler is not None:
        profiler.add("T2", perf_counter() - scheduling_started)
    if local_feasible != evaluation.feasible and evaluation.hard_violation_count == 0:
        raise AssertionError("运输预检查与问题二正式 evaluator 结果不一致")
    if not evaluation.feasible:
        return evaluation, tuple()

    trajectory_started = perf_counter()
    sigma_order = {
        sortie: index
        for index, sortie in enumerate(solution.transport_sorties, start=1)
    }
    delivery_rows = [
        {
            "Box_ID": box_id,
            "Sortie_ID": scheduled.sortie_id,
            "Delivery_Time_s": scheduled.start_s
            + scheduled.metrics.delivery_map[box_id],
        }
        for scheduled in evaluation.scheduled
        for box_id in scheduled.sortie.box_ids
    ]
    delivery_table = pd.DataFrame(delivery_rows)
    records = tuple(
        core.TransportSortieRecord(
            sortie_id=scheduled.sortie_id,
            schedule_order=sigma_order[scheduled.sortie],
            sortie=scheduled.sortie,
            uav_id=scheduled.uav_id,
            battery_id=scheduled.battery_id,
            start_time_s=scheduled.start_s,
            takeoff_time_s=scheduled.departure_s,
            return_time_s=scheduled.completion_s,
            reported_duration_s=scheduled.metrics.duration_s,
        )
        for scheduled in evaluation.scheduled
    )
    trajectories = tuple(
        core.build_transport_trajectory(
            record,
            context.transport_data,
            context.nodes,
            context.cruise_altitude,
            delivery_table,
        )
        for record in records
    )
    if profiler is not None:
        profiler.add("T3", perf_counter() - trajectory_started)
    return evaluation, trajectories


class GeometryCommunicationCache:
    """跨 evaluation 复用 UG 轨迹剖面、UR 空间状态、RG 与飞行静态量。"""

    def __init__(self, context: PhaseAContext) -> None:
        self.context = context
        self.ug_trajectory_profiles: Dict[
            Tuple[Tuple[object, ...], ...], Tuple[DirectOutageProfile, ...]
        ] = {}
        self.ug_position_links: Dict[
            Tuple[float, float, float], communication.LinkEvaluation
        ] = {}
        self.ur_state_covering_candidates: Dict[
            Tuple[float, float, float], frozenset[str]
        ] = {}
        self.rg_static: Dict[str, Tuple[bool, float, int]] = {}
        self.relay_flight_static: Dict[str, baseline.RelayPosition] = {}
        self.ug_profile_hits = 0
        self.ug_profile_misses = 0
        self.ur_state_hits = 0
        self.ur_state_misses = 0
        self.segment_geometry_cache: Optional[
            segment_cache.CommunicationGeometryCache
        ] = None
        self.lazy_segment_cache: Optional[
            segment_cache.LazyPersistentCommunicationCache
        ] = None
        self.segment_ug_hits = 0
        self.segment_ur_hits = 0

    def attach_segment_geometry_cache(
        self, value: segment_cache.CommunicationGeometryCache
    ) -> None:
        self.lazy_segment_cache = None
        self.segment_geometry_cache = value

    def attach_lazy_segment_geometry_cache(
        self, value: segment_cache.LazyPersistentCommunicationCache
    ) -> None:
        self.lazy_segment_cache = value
        self.segment_geometry_cache = value.cache

    def _segment_profile(
        self, origin: str, destination: str
    ) -> segment_cache.SegmentCommunicationProfile:
        if self.lazy_segment_cache is not None:
            profile = self.lazy_segment_cache.ensure(origin, destination)
            # ensure()以不可变dataclass替换cache，保持当前视图同步。
            self.segment_geometry_cache = self.lazy_segment_cache.cache
            return profile
        cache = self.segment_geometry_cache
        if cache is None:
            raise RuntimeError("segment communication cache尚未挂载")
        return cache.segment(origin, destination)

    @staticmethod
    def _point_key(point: communication.Point3D) -> Tuple[float, float, float]:
        return (
            round(point.longitude_deg, 10),
            round(point.latitude_deg, 10),
            round(point.altitude_m, 6),
        )

    @staticmethod
    def _trajectory_key(
        trajectory: core.TransportTrajectory,
    ) -> Tuple[Tuple[object, ...], ...]:
        origin = trajectory.communication_start_s
        return tuple(
            (
                phase.phase,
                phase.start_node,
                phase.end_node,
                round(phase.start_time_s - origin, 6),
                round(phase.end_time_s - origin, 6),
                round(phase.start_point.longitude_deg, 10),
                round(phase.start_point.latitude_deg, 10),
                round(phase.start_point.altitude_m, 6),
                round(phase.end_point.longitude_deg, 10),
                round(phase.end_point.latitude_deg, 10),
                round(phase.end_point.altitude_m, 6),
            )
            for phase in trajectory.communication_phases
        )

    def direct_outages(
        self, trajectories: Sequence[core.TransportTrajectory]
    ) -> Tuple[core.OutageInterval, ...]:
        if self.segment_geometry_cache is not None:
            return self._direct_outages_from_segment_profiles(trajectories)
        provisional: List[core.OutageInterval] = []
        for trajectory in trajectories:
            key = self._trajectory_key(trajectory)
            profile = self.ug_trajectory_profiles.get(key)
            if profile is None:
                self.ug_profile_misses += 1
                _, calculated = core.check_direct_communication(
                    (trajectory,),
                    self.context.gateway,
                    self.context.communication_parameters,
                    self.context.dem,
                    shared_position_cache=self.ug_position_links,
                )
                origin = trajectory.communication_start_s
                profile = tuple(
                    DirectOutageProfile(
                        relative_start_s=row.start_time_s - origin,
                        relative_end_s=row.end_time_s - origin,
                        start_phase=row.start_phase,
                        end_phase=row.end_phase,
                    )
                    for row in calculated
                )
                self.ug_trajectory_profiles[key] = profile
            else:
                self.ug_profile_hits += 1
            origin = trajectory.communication_start_s
            for row in profile:
                start = origin + row.relative_start_s
                end = origin + row.relative_end_s
                provisional.append(
                    core.OutageInterval(
                        outage_id="",
                        transport_uav_id=trajectory.record.uav_id,
                        sortie_id=trajectory.record.sortie_id,
                        start_time_s=start,
                        end_time_s=end,
                        duration_s=end - start,
                        start_phase=row.start_phase,
                        end_phase=row.end_phase,
                        trajectory=trajectory,
                    )
                )
        return tuple(
            core.OutageInterval(
                outage_id=f"OUT-{index:04d}",
                transport_uav_id=row.transport_uav_id,
                sortie_id=row.sortie_id,
                start_time_s=row.start_time_s,
                end_time_s=row.end_time_s,
                duration_s=row.duration_s,
                start_phase=row.start_phase,
                end_phase=row.end_phase,
                trajectory=row.trajectory,
            )
            for index, row in enumerate(
                sorted(provisional, key=lambda item: (item.start_time_s, item.sortie_id)),
                start=1,
            )
        )

    def _direct_outages_from_segment_profiles(
        self, trajectories: Sequence[core.TransportTrajectory]
    ) -> Tuple[core.OutageInterval, ...]:
        cache = self.segment_geometry_cache
        assert cache is not None
        provisional: List[core.OutageInterval] = []
        required_pairs = {
            (phase.start_node, phase.end_node)
            for trajectory in trajectories
            for phase in trajectory.communication_phases
            if phase.phase != core.HANDOVER
            and phase.duration_s > core.TIME_TOLERANCE_S
        }
        profiles = {
            pair: self._segment_profile(*pair) for pair in sorted(required_pairs)
        }
        for trajectory in trajectories:
            outage_start: Optional[float] = None
            for phase in trajectory.communication_phases:
                duration = phase.duration_s
                if duration <= core.TIME_TOLERANCE_S:
                    continue
                if phase.phase == core.HANDOVER:
                    available = cache.ug_handover[phase.start_node]
                    relative_intervals = ((0.0, 1.0, available),)
                else:
                    profile = profiles[
                        (phase.start_node, phase.end_node)
                    ].ug_by_phase[phase.phase]
                    relative_intervals = profile.intervals()
                for q_left, q_right, available in relative_intervals:
                    left = phase.start_time_s + q_left * duration
                    right = phase.start_time_s + q_right * duration
                    if not available and outage_start is None:
                        outage_start = left
                    elif available and outage_start is not None:
                        if left > outage_start + core.TIME_TOLERANCE_S:
                            start_probe = min(
                                left,
                                outage_start
                                + communication.STATE_TRANSITION_TOLERANCE_S,
                            )
                            end_probe = max(
                                outage_start,
                                left
                                - communication.STATE_TRANSITION_TOLERANCE_S,
                            )
                            provisional.append(
                                core.OutageInterval(
                                    outage_id="",
                                    transport_uav_id=trajectory.record.uav_id,
                                    sortie_id=trajectory.record.sortie_id,
                                    start_time_s=outage_start,
                                    end_time_s=left,
                                    duration_s=left - outage_start,
                                    start_phase=trajectory.phase_at(start_probe).phase,
                                    end_phase=trajectory.phase_at(end_probe).phase,
                                    trajectory=trajectory,
                                )
                            )
                        outage_start = None
            if outage_start is not None:
                end = trajectory.communication_end_s
                provisional.append(
                    core.OutageInterval(
                        outage_id="",
                        transport_uav_id=trajectory.record.uav_id,
                        sortie_id=trajectory.record.sortie_id,
                        start_time_s=outage_start,
                        end_time_s=end,
                        duration_s=end - outage_start,
                        start_phase=trajectory.phase_at(
                            min(
                                end,
                                outage_start
                                + communication.STATE_TRANSITION_TOLERANCE_S,
                            )
                        ).phase,
                        end_phase=trajectory.phase_at(
                            max(outage_start, end - core.TIME_TOLERANCE_S)
                        ).phase,
                        trajectory=trajectory,
                    )
                )
            self.segment_ug_hits += len(trajectory.communication_phases)
        return tuple(
            core.OutageInterval(
                outage_id=f"OUT-{index:04d}",
                transport_uav_id=row.transport_uav_id,
                sortie_id=row.sortie_id,
                start_time_s=row.start_time_s,
                end_time_s=row.end_time_s,
                duration_s=row.duration_s,
                start_phase=row.start_phase,
                end_phase=row.end_phase,
                trajectory=row.trajectory,
            )
            for index, row in enumerate(
                sorted(
                    provisional,
                    key=lambda item: (item.start_time_s, item.sortie_id),
                ),
                start=1,
            )
        )

    def relay_coverage(
        self,
        candidates: Sequence[relay.RelayCandidate],
        demands: Sequence[core.CommunicationDemand],
        worker_count: int = DEFAULT_COVERAGE_WORKERS,
    ) -> relay.SpatialDemandStates:
        if self.segment_geometry_cache is not None and all(
            demand.segment_origin and demand.segment_destination
            for demand in demands
        ):
            return self._relay_coverage_from_segment_profiles(candidates, demands)
        # Step 4 的聚合器会断言 baseline 固定总时长；联合搜索的需求会变化，
        # 因此这里使用完全相同的坐标键口径，但不附加 baseline 专用断言。
        states = _aggregate_dynamic_spatial_states(demands)
        state_keys = tuple(
            (
                round(float(lon), 10),
                round(float(lat), 10),
                round(float(alt), 6),
            )
            for lon, lat, alt in zip(
                states.longitude_deg, states.latitude_deg, states.altitude_m
            )
        )
        cached = [self.ur_state_covering_candidates.get(key) for key in state_keys]
        missing = [index for index, value in enumerate(cached) if value is None]
        self.ur_state_hits += len(state_keys) - len(missing)
        self.ur_state_misses += len(missing)
        if missing:
            indexes = np.asarray(missing, dtype=np.int32)
            missing_states = relay.SpatialDemandStates(
                longitude_deg=states.longitude_deg[indexes],
                latitude_deg=states.latitude_deg[indexes],
                altitude_m=states.altitude_m[indexes],
                projected_x_m=states.projected_x_m[indexes],
                projected_y_m=states.projected_y_m[indexes],
                duration_weight_s=states.duration_weight_s[indexes],
                demand_count=states.demand_count[indexes],
                member_demand_indices=tuple(
                    states.member_demand_indices[int(index)] for index in indexes
                ),
                outage_ids=tuple(states.outage_ids[int(index)] for index in indexes),
                uav_ids=tuple(states.uav_ids[int(index)] for index in indexes),
            )
            relay.precompute_ur_coverage(
                candidates, missing_states, worker_count=worker_count
            )
            covering_by_missing: List[set[str]] = [
                set() for _ in range(missing_states.count)
            ]
            for candidate in candidates:
                for state_index in candidate.covered_state_indices:
                    covering_by_missing[int(state_index)].add(candidate.candidate_id)
            for full_index, covering in zip(missing, covering_by_missing):
                frozen = frozenset(covering)
                self.ur_state_covering_candidates[state_keys[full_index]] = frozen
                cached[full_index] = frozen

        # precompute_ur_coverage 会把候选索引改成缺失子集的局部索引；始终从
        # 统一 cache 重新构造当前完整 states 的覆盖向量。
        for candidate in candidates:
            candidate.covered_state_indices = np.asarray(
                [
                    index
                    for index, covering in enumerate(cached)
                    if covering is not None and candidate.candidate_id in covering
                ],
                dtype=np.int32,
            )
            _update_candidate_coverage_statistics(candidate, states)
        return states

    def _relay_coverage_from_segment_profiles(
        self,
        candidates: Sequence[relay.RelayCandidate],
        demands: Sequence[core.CommunicationDemand],
    ) -> relay.SpatialDemandStates:
        cache = self.segment_geometry_cache
        assert cache is not None
        candidate_index = cache.candidate_index()
        if any(row.candidate_id not in candidate_index for row in candidates):
            raise ValueError("segment cache 与当前 elite candidate pool 不一致")
        states = _aggregate_dynamic_spatial_states(demands)
        required_pairs = {
            (demand.segment_origin, demand.segment_destination)
            for demand in demands
            if demand.phase != core.HANDOVER
        }
        profiles = {
            pair: self._segment_profile(*pair) for pair in sorted(required_pairs)
        }
        for candidate in candidates:
            cache_index = candidate_index[candidate.candidate_id]
            covered: List[int] = []
            for state_index, members in enumerate(states.member_demand_indices):
                demand = demands[int(members[0])]
                if demand.phase == core.HANDOVER:
                    available = cache.ur_handover[demand.segment_origin][cache_index]
                else:
                    phase_duration = (
                        demand.phase_end_time_s - demand.phase_start_time_s
                    )
                    q = (
                        0.0
                        if phase_duration <= core.TIME_TOLERANCE_S
                        else (
                            demand.sample_time_s - demand.phase_start_time_s
                        )
                        / phase_duration
                    )
                    available = profiles[
                        (demand.segment_origin, demand.segment_destination)
                    ].ur_by_phase[demand.phase][cache_index].available_at(q)
                if available:
                    covered.append(state_index)
            candidate.covered_state_indices = np.asarray(
                covered, dtype=np.int32
            )
            _update_candidate_coverage_statistics(candidate, states)
        self.segment_ur_hits += states.count * len(candidates)
        return states

    def manifest(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "Cache": "RG static",
                    "Key": "relay_candidate_id",
                    "Stored_Value": "availability/path loss/obstruction",
                    "Entries": len(self.rg_static),
                },
                {
                    "Cache": "Relay flight static",
                    "Key": "relay_candidate_id",
                    "Stored_Value": "flight altitude/time/energy/max service",
                    "Entries": len(self.relay_flight_static),
                },
                {
                    "Cache": "UG trajectory profile",
                    "Key": "relative 3D trajectory phase geometry",
                    "Stored_Value": "relative direct-outage transition intervals",
                    "Entries": len(self.ug_trajectory_profiles),
                    "Hits": self.ug_profile_hits,
                    "Misses": self.ug_profile_misses,
                },
                {
                    "Cache": "UG 3D endpoint link",
                    "Key": "rounded UAV longitude/latitude/absolute altitude",
                    "Stored_Value": "deterministic UG link evaluation",
                    "Entries": len(self.ug_position_links),
                },
                {
                    "Cache": "UR spatial state",
                    "Key": "rounded UAV longitude/latitude/absolute altitude",
                    "Stored_Value": "covering elite candidate IDs",
                    "Entries": len(self.ur_state_covering_candidates),
                    "Hits": self.ur_state_hits,
                    "Misses": self.ur_state_misses,
                },
                {
                    "Cache": "Fixed segment communication profile",
                    "Key": "directed segment/phase/q/candidate",
                    "Stored_Value": "UG/UR binary transition profile",
                    "Entries": (
                        len(self.segment_geometry_cache.segment_profiles)
                        if self.segment_geometry_cache is not None
                        else 0
                    ),
                    "Hits": self.segment_ug_hits + self.segment_ur_hits,
                },
            ]
        )


def _aggregate_dynamic_spatial_states(
    demands: Sequence[core.CommunicationDemand],
) -> relay.SpatialDemandStates:
    """按 Step 4 的精度聚合动态需求，不绑定 baseline 的固定需求总量。"""
    groups: Dict[Tuple[float, float, float], List[int]] = {}
    for index, demand in enumerate(demands):
        key = (
            round(demand.point.longitude_deg, relay.STATE_KEY_DECIMALS[0]),
            round(demand.point.latitude_deg, relay.STATE_KEY_DECIMALS[1]),
            round(demand.point.altitude_m, relay.STATE_KEY_DECIMALS[2]),
        )
        groups.setdefault(key, []).append(index)
    keys = tuple(groups)
    longitude = np.asarray([key[0] for key in keys], dtype=np.float64)
    latitude = np.asarray([key[1] for key in keys], dtype=np.float64)
    altitude = np.asarray([key[2] for key in keys], dtype=np.float64)
    projected_x, projected_y = relay.PROJECT_TO_LOCAL.transform(
        longitude, latitude
    )
    members = tuple(
        np.asarray(groups[key], dtype=np.int32) for key in keys
    )
    return relay.SpatialDemandStates(
        longitude_deg=longitude,
        latitude_deg=latitude,
        altitude_m=altitude,
        projected_x_m=np.asarray(projected_x, dtype=np.float64),
        projected_y_m=np.asarray(projected_y, dtype=np.float64),
        duration_weight_s=np.asarray(
            [
                sum(demands[i].duration_weight_s for i in indexes)
                for indexes in members
            ],
            dtype=np.float64,
        ),
        demand_count=np.asarray([len(row) for row in members], dtype=np.int32),
        member_demand_indices=members,
        outage_ids=tuple(
            frozenset(demands[i].outage_id for i in indexes)
            for indexes in members
        ),
        uav_ids=tuple(
            frozenset(demands[i].transport_uav_id for i in indexes)
            for indexes in members
        ),
    )


def _update_candidate_coverage_statistics(
    candidate: relay.RelayCandidate,
    states: relay.SpatialDemandStates,
) -> None:
    indexes = candidate.covered_state_indices
    candidate.covered_demand_count = int(states.demand_count[indexes].sum())
    candidate.covered_duration_s = float(states.duration_weight_s[indexes].sum())
    if len(indexes) == 0:
        candidate.covered_outage_count = 0
        candidate.covered_uav_count = 0
        return
    candidate.covered_outage_count = len(
        set().union(*(states.outage_ids[int(index)] for index in indexes))
    )
    candidate.covered_uav_count = len(
        set().union(*(states.uav_ids[int(index)] for index in indexes))
    )


def _relay_position_from_candidate(
    candidate: relay.RelayCandidate,
    position_id: str,
    context: PhaseAContext,
) -> baseline.RelayPosition:
    point = communication.Point3D(
        candidate.longitude_deg,
        candidate.latitude_deg,
        candidate.absolute_altitude_m,
    )
    horizontal_distance = communication.horizontal_distance_m(
        context.gateway.point, point
    )
    parameters = context.relay_parameters
    outbound_time = (
        candidate.outbound_climb_height_m / parameters.climb_speed_m_s
        + horizontal_distance / parameters.cruise_speed_m_s
        + candidate.outbound_descent_height_m / parameters.descent_speed_m_s
    )
    return_time = (
        candidate.return_climb_height_m / parameters.climb_speed_m_s
        + horizontal_distance / parameters.cruise_speed_m_s
        + candidate.return_descent_height_m / parameters.descent_speed_m_s
    )
    horizontal_energy = (
        parameters.cruise_power_kw
        * horizontal_distance
        / parameters.cruise_speed_m_s
        / 3600.0
    )
    outbound_energy = horizontal_energy + baseline._climb_energy_kwh(
        candidate.outbound_climb_height_m, parameters
    )
    return_energy = horizontal_energy + baseline._climb_energy_kwh(
        candidate.return_climb_height_m, parameters
    )
    maximum_service = (
        parameters.sortie_energy_limit_kwh - outbound_energy - return_energy
    ) / parameters.service_power_kw * 3600.0
    if maximum_service <= 0.0:
        raise ValueError(f"中继候选{candidate.candidate_id}往返飞行后无可用服务能量")
    return baseline.RelayPosition(
        position_id=position_id,
        candidate_id=candidate.candidate_id,
        longitude_deg=candidate.longitude_deg,
        latitude_deg=candidate.latitude_deg,
        ground_elevation_m=candidate.ground_elevation_m,
        hover_height_agl_m=candidate.hover_height_m,
        hover_absolute_altitude_m=candidate.absolute_altitude_m,
        segment_maximum_ground_elevation_m=(
            candidate.segment_maximum_ground_elevation_m
        ),
        terrain_clearance_altitude_m=candidate.terrain_clearance_altitude_m,
        actual_flight_altitude_m=candidate.actual_flight_altitude_m,
        horizontal_distance_m=horizontal_distance,
        outbound_climb_height_m=candidate.outbound_climb_height_m,
        outbound_descent_height_m=candidate.outbound_descent_height_m,
        return_climb_height_m=candidate.return_climb_height_m,
        return_descent_height_m=candidate.return_descent_height_m,
        outbound_flight_time_s=outbound_time,
        return_flight_time_s=return_time,
        outbound_energy_kwh=outbound_energy,
        return_energy_kwh=return_energy,
        maximum_service_time_s=maximum_service,
        endpoint=point,
    )


def _candidate_from_row(row: object) -> relay.RelayCandidate:
    source_text = str(getattr(row, "Candidate_Sources", ""))
    sources = tuple(value for value in source_text.split(";") if value and value != "nan")
    return relay.RelayCandidate(
        candidate_id=str(row.Candidate_ID),
        level=str(row.Level),
        site_key=str(row.Site_Key),
        longitude_deg=float(row.Longitude),
        latitude_deg=float(row.Latitude),
        ground_elevation_m=float(row.Ground_Elevation_m),
        hover_height_m=float(row.Hover_Height_m),
        absolute_altitude_m=float(row.Absolute_Altitude_m),
        sources=sources,
        segment_maximum_ground_elevation_m=float(
            row.Segment_Maximum_Ground_Elevation_m
        ),
        terrain_clearance_altitude_m=float(row.Terrain_Clearance_Altitude_m),
        actual_flight_altitude_m=float(row.Actual_Flight_Altitude_m),
        outbound_climb_height_m=float(row.Outbound_Climb_Height_m),
        outbound_descent_height_m=float(row.Outbound_Descent_Height_m),
        return_climb_height_m=float(row.Return_Climb_Height_m),
        return_descent_height_m=float(row.Return_Descent_Height_m),
        rg_distance_km=float(row.RG_Distance_km),
        rg_obstruction=int(row.RG_Obstruction),
        rg_fspl_db=float(row.RG_FSPL_dB),
        rg_path_loss_db=float(row.RG_Path_Loss_dB),
        rg_maximum_path_loss_db=float(row.RG_Lmax_dB),
        rg_available=bool(row.RG_Available),
        covered_demand_count=int(row.Covered_Demand_Count),
        covered_duration_s=float(row.Covered_Duration_s),
        covered_outage_count=int(row.Covered_Outage_Count),
        covered_uav_count=int(row.Covered_UAV_Count),
    )


def _static_candidate_table(context: PhaseAContext) -> pd.DataFrame:
    point_columns = [
        "Candidate_ID", "Level", "Site_Key", "Longitude", "Latitude",
        "Ground_Elevation_m", "Hover_Height_m", "Absolute_Altitude_m",
        "Segment_Maximum_Ground_Elevation_m", "Terrain_Clearance_Altitude_m",
        "Actual_Flight_Altitude_m", "Outbound_Climb_Height_m",
        "Outbound_Descent_Height_m", "Return_Climb_Height_m",
        "Return_Descent_Height_m", "Candidate_Sources", "RG_Distance_km",
        "RG_Obstruction", "RG_FSPL_dB", "RG_Path_Loss_dB", "RG_Lmax_dB",
        "RG_Available",
    ]
    summary_columns = [
        "Candidate_ID", "Covered_Demand_Count", "Covered_Duration_s",
        "Covered_Percent", "Covered_Outage_Count", "Covered_UAV_Count",
    ]
    points = pd.read_excel(paths.require_file(STEP4_FILE), sheet_name="Candidate_Points", usecols=point_columns)
    summary = pd.read_excel(
        paths.require_file(STEP4_FILE), sheet_name="Candidate_Coverage_Summary", usecols=summary_columns
    )
    table = points.merge(summary, on="Candidate_ID", how="left")
    table = table.loc[table["RG_Available"].eq(1)].copy()
    if len(table) != 18_016:
        raise AssertionError(f"RG可用候选应为18016，实际为{len(table)}")

    lon = table["Longitude"].to_numpy(float)
    lat = table["Latitude"].to_numpy(float)
    x_m, y_m = relay.PROJECT_TO_LOCAL.transform(lon, lat)
    table["Projected_X_m"] = x_m
    table["Projected_Y_m"] = y_m
    distances = np.asarray(
        [
            communication.horizontal_distance_m(
                context.gateway.point,
                communication.Point3D(row.Longitude, row.Latitude, row.Absolute_Altitude_m),
            )
            for row in table.itertuples(index=False)
        ],
        dtype=float,
    )
    parameters = context.relay_parameters
    horizontal_energy = (
        parameters.cruise_power_kw
        * distances
        / parameters.cruise_speed_m_s
        / 3600.0
    )
    climb_factor = (
        parameters.planned_mass_kg
        * baseline.GRAVITY_M_S2
        / (parameters.climb_efficiency * baseline.JOULES_PER_KWH)
    )
    table["Horizontal_Distance_m"] = distances
    table["Outbound_Flight_Time_s"] = (
        table["Outbound_Climb_Height_m"] / parameters.climb_speed_m_s
        + distances / parameters.cruise_speed_m_s
        + table["Outbound_Descent_Height_m"] / parameters.descent_speed_m_s
    )
    table["Return_Flight_Time_s"] = (
        table["Return_Climb_Height_m"] / parameters.climb_speed_m_s
        + distances / parameters.cruise_speed_m_s
        + table["Return_Descent_Height_m"] / parameters.descent_speed_m_s
    )
    table["Outbound_Energy_kWh"] = (
        horizontal_energy + table["Outbound_Climb_Height_m"] * climb_factor
    )
    table["Return_Energy_kWh"] = (
        horizontal_energy + table["Return_Climb_Height_m"] * climb_factor
    )
    table["Roundtrip_Energy_kWh"] = (
        table["Outbound_Energy_kWh"] + table["Return_Energy_kWh"]
    )
    table["Arrival_Lead_Time_s"] = (
        parameters.prepare_time_s
        + table["Outbound_Flight_Time_s"]
        + parameters.link_setup_time_s
    )
    table["Maximum_Service_Time_s"] = (
        parameters.sortie_energy_limit_kwh - table["Roundtrip_Energy_kWh"]
    ) / parameters.service_power_kw * 3600.0
    if (table["Maximum_Service_Time_s"] <= 0.0).any():
        raise AssertionError("RG可用候选中存在往返能量已经超限的方案")

    coverage_scale = max(float(table["Covered_Duration_s"].max()), 1.0)
    energy_min, energy_max = table["Roundtrip_Energy_kWh"].agg(["min", "max"])
    lead_min, lead_max = table["Arrival_Lead_Time_s"].agg(["min", "max"])
    service_min, service_max = table["Maximum_Service_Time_s"].agg(["min", "max"])
    table["Coverage_Score"] = table["Covered_Duration_s"] / coverage_scale
    table["Energy_Score"] = 1.0 - (
        table["Roundtrip_Energy_kWh"] - energy_min
    ) / max(energy_max - energy_min, NUMERIC_TOLERANCE)
    table["Lead_Score"] = 1.0 - (
        table["Arrival_Lead_Time_s"] - lead_min
    ) / max(lead_max - lead_min, NUMERIC_TOLERANCE)
    table["Service_Score"] = (
        table["Maximum_Service_Time_s"] - service_min
    ) / max(service_max - service_min, NUMERIC_TOLERANCE)
    table["Static_Utility"] = (
        0.65 * table["Coverage_Score"]
        + 0.15 * table["Energy_Score"]
        + 0.10 * table["Lead_Score"]
        + 0.10 * table["Service_Score"]
    )
    return table.reset_index(drop=True)


def _select_diverse_ids(
    table: pd.DataFrame,
    target_size: int,
    seed_ids: Iterable[str],
    utility_column: str,
) -> Tuple[str, ...]:
    selected = list(dict.fromkeys(str(value) for value in seed_ids))
    selected_set = set(selected)
    if len(selected) >= target_size:
        return tuple(selected[:target_size])

    working = table.loc[~table["Candidate_ID"].isin(selected_set)].copy()
    working = working.nlargest(min(5000, len(working)), utility_column)
    vectors = np.column_stack(
        (
            working["Projected_X_m"].to_numpy(float) / 1000.0,
            working["Projected_Y_m"].to_numpy(float) / 1000.0,
            working["Hover_Height_m"].to_numpy(float) / 100.0,
        )
    )
    utility = working[utility_column].to_numpy(float)
    if selected:
        seed_table = table.set_index("Candidate_ID").loc[selected]
        seed_vectors = np.column_stack(
            (
                seed_table["Projected_X_m"].to_numpy(float) / 1000.0,
                seed_table["Projected_Y_m"].to_numpy(float) / 1000.0,
                seed_table["Hover_Height_m"].to_numpy(float) / 100.0,
            )
        )
        minimum_distance = np.min(
            ((vectors[:, None, :] - seed_vectors[None, :, :]) ** 2).sum(axis=2),
            axis=1,
        )
    else:
        minimum_distance = np.ones(len(working), dtype=float)
    available = np.ones(len(working), dtype=bool)
    ids = working["Candidate_ID"].astype(str).to_numpy()
    while len(selected) < target_size and bool(np.any(available)):
        score = minimum_distance * (0.25 + utility)
        score[~available] = -math.inf
        index = int(np.argmax(score))
        selected.append(ids[index])
        selected_set.add(ids[index])
        available[index] = False
        distance = ((vectors - vectors[index]) ** 2).sum(axis=1)
        minimum_distance = np.minimum(minimum_distance, distance)
    return tuple(selected)


def _initial_elite_ids(table: pd.DataFrame) -> Tuple[str, ...]:
    seeds: List[str] = list(BASELINE_CANDIDATE_IDS)
    seeds.extend(
        table.nlargest(ELITE_COVERAGE_SEED_COUNT, "Covered_Duration_s")[
            "Candidate_ID"
        ].astype(str)
    )
    positive = table.loc[table["Covered_Duration_s"] > 0.0]
    seeds.extend(
        positive.nsmallest(ELITE_LOW_ENERGY_SEED_COUNT, "Roundtrip_Energy_kWh")[
            "Candidate_ID"
        ].astype(str)
    )
    seeds.extend(
        positive.nsmallest(ELITE_LOW_ENERGY_SEED_COUNT, "Arrival_Lead_Time_s")[
            "Candidate_ID"
        ].astype(str)
    )
    for _, rows in table.groupby("Hover_Height_m"):
        seeds.extend(
            rows.nlargest(ELITE_PER_HEIGHT_SEED_COUNT, "Covered_Duration_s")[
                "Candidate_ID"
            ].astype(str)
        )
    return _select_diverse_ids(
        table, ELITE_PRESELECTION_SIZE, seeds, "Static_Utility"
    )


def build_elite_pool(
    context: PhaseAContext,
    cache: GeometryCommunicationCache,
    baseline_demands: Sequence[core.CommunicationDemand],
    worker_count: int = DEFAULT_COVERAGE_WORKERS,
) -> ElitePool:
    started = perf_counter()
    static_table = _static_candidate_table(context)
    cache.rg_static.update(
        {
            str(row.Candidate_ID): (
                bool(row.RG_Available),
                float(row.RG_Path_Loss_dB),
                int(row.RG_Obstruction),
            )
            for row in static_table.itertuples(index=False)
        }
    )
    initial_ids = _initial_elite_ids(static_table)
    by_id = static_table.set_index("Candidate_ID", drop=False)
    pre_candidates = tuple(
        _candidate_from_row(by_id.loc[candidate_id]) for candidate_id in initial_ids
    )
    states = cache.relay_coverage(
        pre_candidates, baseline_demands, worker_count=worker_count
    )

    covering_count = np.zeros(states.count, dtype=float)
    for candidate in pre_candidates:
        covering_count[candidate.covered_state_indices] += 1.0
    safe_count = np.maximum(covering_count, 1.0)
    rare_scores = {
        candidate.candidate_id: float(
            (states.duration_weight_s[candidate.covered_state_indices]
             / safe_count[candidate.covered_state_indices]).sum()
        )
        for candidate in pre_candidates
    }
    initial_table = by_id.loc[list(initial_ids)].copy()
    initial_table["Rare_Coverage_Score_s"] = initial_table.index.map(rare_scores)
    max_rare = max(float(initial_table["Rare_Coverage_Score_s"].max()), 1.0)
    initial_table["Elite_Utility"] = (
        0.55 * initial_table["Coverage_Score"]
        + 0.20 * initial_table["Rare_Coverage_Score_s"] / max_rare
        + 0.10 * initial_table["Energy_Score"]
        + 0.075 * initial_table["Lead_Score"]
        + 0.075 * initial_table["Service_Score"]
    )
    seeds: List[str] = list(BASELINE_CANDIDATE_IDS)
    seeds.extend(
        initial_table.nlargest(ELITE_COVERAGE_SEED_COUNT, "Covered_Duration_s")[
            "Candidate_ID"
        ].astype(str)
    )
    seeds.extend(
        initial_table.nlargest(ELITE_RARE_SEED_COUNT, "Rare_Coverage_Score_s")[
            "Candidate_ID"
        ].astype(str)
    )
    seeds.extend(
        initial_table.nsmallest(
            ELITE_LOW_ENERGY_SEED_COUNT, "Roundtrip_Energy_kWh"
        )["Candidate_ID"].astype(str)
    )
    for _, rows in initial_table.groupby("Hover_Height_m"):
        seeds.extend(
            rows.nlargest(ELITE_PER_HEIGHT_SEED_COUNT, "Elite_Utility")[
                "Candidate_ID"
            ].astype(str)
        )
    final_ids = _select_diverse_ids(
        initial_table.reset_index(drop=True),
        ELITE_TARGET_SIZE,
        seeds,
        "Elite_Utility",
    )
    candidate_by_id = {row.candidate_id: row for row in pre_candidates}
    candidates = tuple(candidate_by_id[value] for value in final_ids)
    final_table = initial_table.loc[list(final_ids)].copy().reset_index(drop=True)
    final_table.insert(0, "Elite_Rank", np.arange(1, len(final_table) + 1))
    final_table["Mandatory_Baseline"] = final_table["Candidate_ID"].isin(
        BASELINE_CANDIDATE_IDS
    )

    covered = np.zeros(states.count, dtype=bool)
    for candidate in candidates:
        covered[candidate.covered_state_indices] = True
        position_id = BASELINE_POSITION_IDS.get(
            candidate.candidate_id, f"ELITE-{len(cache.relay_flight_static) + 1:03d}"
        )
        cache.relay_flight_static[candidate.candidate_id] = (
            _relay_position_from_candidate(candidate, position_id, context)
        )
    if not bool(np.all(covered)):
        missing = float(states.duration_weight_s[~covered].sum())
        raise RuntimeError(f"elite pool未覆盖baseline通信需求，缺口={missing:.9f}s")
    if not set(BASELINE_CANDIDATE_IDS).issubset(
        {row.candidate_id for row in candidates}
    ):
        raise AssertionError("elite pool未保留全部baseline P1/P2/P3")

    return ElitePool(
        candidates=candidates,
        fallback_rg_usable=static_table,
        baseline_states=states,
        selection_table=final_table,
        static_cache_table=cache.manifest(),
        preprocessing_runtime_s=perf_counter() - started,
    )


def _run_length_decode(value: object) -> np.ndarray:
    """恢复 Phase A 工作簿内保存的覆盖状态索引。"""
    text = "" if pd.isna(value) else str(value).strip()
    if not text:
        return np.empty(0, dtype=np.int32)
    result: List[int] = []
    for token in text.split(";"):
        if "-" in token:
            start, end = (int(part) for part in token.split("-", 1))
            result.extend(range(start, end + 1))
        else:
            result.append(int(token))
    return np.asarray(result, dtype=np.int32)


def load_saved_elite_pool(
    context: PhaseAContext,
    cache: GeometryCommunicationCache,
    baseline_demands: Sequence[core.CommunicationDemand],
    path: Path = ELITE_POOL_FILE,
) -> ElitePool:
    """读取已审计的 Phase A elite pool，并恢复静态/基准覆盖缓存。"""
    selection = pd.read_excel(paths.require_file(path), sheet_name="Elite_Candidates")
    fallback = pd.read_excel(paths.require_file(path), sheet_name="Fallback_RG_Usable")
    candidates: List[relay.RelayCandidate] = []
    for row in selection.itertuples(index=False):
        candidate = _candidate_from_row(row)
        candidate.covered_state_indices = _run_length_decode(
            getattr(row, "Coverage_State_Ranges")
        )
        candidates.append(candidate)

    states = _aggregate_dynamic_spatial_states(baseline_demands)
    covering: List[set[str]] = [set() for _ in range(states.count)]
    for candidate in candidates:
        if len(candidate.covered_state_indices) and int(
            candidate.covered_state_indices.max()
        ) >= states.count:
            raise ValueError(
                f"候选{candidate.candidate_id}的基准覆盖索引超出空间状态范围"
            )
        for state_index in candidate.covered_state_indices:
            covering[int(state_index)].add(candidate.candidate_id)
        _update_candidate_coverage_statistics(candidate, states)
        position_id = BASELINE_POSITION_IDS.get(
            candidate.candidate_id,
            f"ELITE-{len(cache.relay_flight_static) + 1:03d}",
        )
        cache.relay_flight_static[candidate.candidate_id] = (
            _relay_position_from_candidate(candidate, position_id, context)
        )

    for lon, lat, alt, candidate_ids in zip(
        states.longitude_deg,
        states.latitude_deg,
        states.altitude_m,
        covering,
    ):
        cache.ur_state_covering_candidates[
            (round(float(lon), 10), round(float(lat), 10), round(float(alt), 6))
        ] = frozenset(candidate_ids)
    cache.rg_static.update(
        {
            str(row.Candidate_ID): (
                bool(row.RG_Available),
                float(row.RG_Path_Loss_dB),
                int(row.RG_Obstruction),
            )
            for row in fallback.itertuples(index=False)
        }
    )
    return ElitePool(
        candidates=tuple(candidates),
        fallback_rg_usable=fallback,
        baseline_states=states,
        selection_table=selection,
        static_cache_table=cache.manifest(),
        preprocessing_runtime_s=0.0,
    )


def attach_persistent_segment_cache_if_available(
    context: PhaseAContext,
    geometry_cache: GeometryCommunicationCache,
    elite_pool: ElitePool,
) -> bool:
    """签名一致时加载 Phase A.5 缓存；否则保持原逐点逻辑作为 fallback。"""
    if not segment_cache.CACHE_FILE.exists():
        if not paths.cache_input_file().exists():
            return False
        paths.prepare_cache(segment_cache.CACHE_FILE)
    value = segment_cache.load_cache(segment_cache.CACHE_FILE)
    segments = segment_cache.actual_possible_directed_segments(
        context.transport_data
    )
    expected = segment_cache.cache_signature(
        context, elite_pool.candidates, segments
    )
    if value.signature != expected:
        raise ValueError("Phase A.5 通信几何缓存签名与当前数据/elite pool不一致")
    geometry_cache.attach_segment_geometry_cache(value)
    return True


def attach_lazy_persistent_segment_cache(
    context: PhaseAContext,
    geometry_cache: GeometryCommunicationCache,
    elite_pool: ElitePool,
) -> segment_cache.LazyPersistentCommunicationCache:
    """加载严格签名缓存；文件不存在时仅建立HANDOVER状态的空缓存。"""
    manager = segment_cache.LazyPersistentCommunicationCache(
        context,
        elite_pool.candidates,
        segment_cache.CACHE_FILE,
        segment_cache.MANIFEST_FILE,
        worker_count=DEFAULT_COVERAGE_WORKERS,
    )
    geometry_cache.attach_lazy_segment_geometry_cache(manager)
    return manager


def _weighted_cost_aware_greedy(
    candidates: Sequence[relay.RelayCandidate],
    states: relay.SpatialDemandStates,
    positions: Mapping[str, baseline.RelayPosition],
    parameters: baseline.RelayParameters,
) -> Tuple[relay.RelayCandidate, ...]:
    uncovered = np.ones(states.count, dtype=bool)
    coverage_count = np.zeros(states.count, dtype=float)
    for candidate in candidates:
        coverage_count[candidate.covered_state_indices] += 1.0
    rare_weight = states.duration_weight_s / np.maximum(coverage_count, 1.0)
    energy_values = np.asarray(
        [
            positions[row.candidate_id].outbound_energy_kwh
            + positions[row.candidate_id].return_energy_kwh
            for row in candidates
        ]
    )
    lead_values = np.asarray(
        [
            baseline_position_lead_time(positions[row.candidate_id], parameters)
            for row in candidates
        ]
    )
    energy_scale = max(float(np.median(energy_values)), NUMERIC_TOLERANCE)
    lead_scale = max(float(np.median(lead_values)), NUMERIC_TOLERANCE)
    selected: List[relay.RelayCandidate] = []
    while bool(np.any(uncovered)):
        best: Optional[relay.RelayCandidate] = None
        best_indexes = np.empty(0, dtype=np.int32)
        best_score = -math.inf
        for candidate in candidates:
            indexes = candidate.covered_state_indices
            new_indexes = indexes[uncovered[indexes]]
            if len(new_indexes) == 0:
                continue
            gain = float(states.duration_weight_s[new_indexes].sum())
            rare_gain = float(rare_weight[new_indexes].sum())
            position = positions[candidate.candidate_id]
            energy = position.outbound_energy_kwh + position.return_energy_kwh
            lead = baseline_position_lead_time(position, parameters)
            service_factor = min(
                1.0,
                position.maximum_service_time_s
                / max(float(states.duration_weight_s.sum()), 1.0),
            )
            score = gain * (1.0 + 0.10 * rare_gain / max(gain, NUMERIC_TOLERANCE))
            score /= (
                1.0
                + 0.08 * energy / energy_scale
                + 0.04 * lead / lead_scale
                + 0.02 * (1.0 - service_factor)
            )
            tied = math.isclose(score, best_score, abs_tol=NUMERIC_TOLERANCE)
            if score > best_score + NUMERIC_TOLERANCE or (
                tied
                and best is not None
                and (
                    candidate.candidate_id not in BASELINE_CANDIDATE_IDS,
                    candidate.hover_height_m,
                    candidate.candidate_id,
                )
                < (
                    best.candidate_id not in BASELINE_CANDIDATE_IDS,
                    best.hover_height_m,
                    best.candidate_id,
                )
            ):
                best = candidate
                best_indexes = new_indexes
                best_score = score
        if best is None:
            missing = float(states.duration_weight_s[uncovered].sum())
            raise baseline.SchedulingConflict(
                f"elite pool无法覆盖当前运输解，通信缺口={missing:.9f}s"
            )
        uncovered[best_indexes] = False
        selected.append(best)
    return tuple(selected)


def baseline_position_lead_time(
    position: baseline.RelayPosition,
    parameters: baseline.RelayParameters,
) -> float:
    return (
        parameters.prepare_time_s
        + position.outbound_flight_time_s
        + parameters.link_setup_time_s
    )


def _coverage_map_for_selected(
    demands: Sequence[core.CommunicationDemand],
    states: relay.SpatialDemandStates,
    selected: Sequence[relay.RelayCandidate],
    candidate_to_position: Mapping[str, str],
) -> Dict[str, Tuple[str, ...]]:
    demand_to_state = np.empty(len(demands), dtype=np.int32)
    for state_index, members in enumerate(states.member_demand_indices):
        demand_to_state[members] = state_index
    covered_sets = {
        row.candidate_id: set(int(value) for value in row.covered_state_indices)
        for row in selected
    }
    result: Dict[str, Tuple[str, ...]] = {}
    for demand_index, demand in enumerate(demands):
        state_index = int(demand_to_state[demand_index])
        covering = tuple(
            candidate_to_position[row.candidate_id]
            for row in selected
            if state_index in covered_sets[row.candidate_id]
        )
        if not covering:
            raise baseline.SchedulingConflict(
                f"需求{demand.demand_id}没有入选中继位置可以覆盖"
            )
        result[demand.demand_id] = covering
    return result


def _cost_aware_sortie_plans(
    tasks: Sequence[baseline.RelayServiceTask],
    positions: Mapping[str, baseline.RelayPosition],
    parameters: baseline.RelayParameters,
) -> Tuple[baseline.RelaySortiePlan, ...]:
    grouped: Dict[str, List[baseline.RelayServiceTask]] = defaultdict(list)
    for task in tasks:
        maximum_piece = (
            positions[task.position_id].maximum_service_time_s
            - 2.0 * baseline.RELAY_SERVICE_BOUNDARY_BUFFER_S
        )
        grouped[task.position_id].extend(baseline._split_task(task, maximum_piece))

    batches: List[Tuple[str, float, float, Tuple[baseline.RelayServiceTask, ...]]] = []
    for position_id, rows in grouped.items():
        rows.sort(key=lambda row: (row.required_start_s, row.required_end_s))
        position = positions[position_id]
        break_even_gap = (
            position.outbound_energy_kwh + position.return_energy_kwh
        ) * 3600.0 / parameters.service_power_kw
        batch: List[baseline.RelayServiceTask] = []
        start = end = math.nan
        for task in rows:
            if not batch:
                batch, start, end = [task], task.required_start_s, task.required_end_s
                continue
            proposed_end = max(end, task.required_end_s)
            service_span = (
                proposed_end - start
                + 2.0 * baseline.RELAY_SERVICE_BOUNDARY_BUFFER_S
            )
            gap = max(0.0, task.required_start_s - end)
            if (
                gap < break_even_gap - NUMERIC_TOLERANCE
                and service_span <= position.maximum_service_time_s + NUMERIC_TOLERANCE
            ):
                batch.append(task)
                end = proposed_end
            else:
                batches.append((position_id, start, end, tuple(batch)))
                batch, start, end = [task], task.required_start_s, task.required_end_s
        if batch:
            batches.append((position_id, start, end, tuple(batch)))

    batches.sort(key=lambda value: (value[1], value[2], value[0]))
    plans: List[baseline.RelaySortiePlan] = []
    for index, (position_id, start, end, rows) in enumerate(batches, start=1):
        plans.append(
            baseline.RelaySortiePlan(
                plan_id=f"RSP-{index:03d}",
                position_id=position_id,
                service_start_s=max(
                    0.0, start - baseline.RELAY_SERVICE_BOUNDARY_BUFFER_S
                ),
                service_end_s=end + baseline.RELAY_SERVICE_BOUNDARY_BUFFER_S,
                task_ids=tuple(row.task_id for row in rows),
                covered_transport_sorties=tuple(
                    sorted(set().union(*(set(row.covered_transport_sorties) for row in rows)))
                ),
                covered_transport_uavs=tuple(
                    sorted(set().union(*(set(row.covered_transport_uavs) for row in rows)))
                ),
                covered_outages=tuple(
                    sorted(set().union(*(set(row.covered_outages) for row in rows)))
                ),
                covered_demand_ids=tuple(
                    sorted(set().union(*(set(row.covered_demand_ids) for row in rows)))
                ),
            )
        )
    return tuple(plans)


def fast_relay_decode(
    demands: Sequence[core.CommunicationDemand],
    elite_pool: ElitePool,
    cache: GeometryCommunicationCache,
    context: PhaseAContext,
    profiler: Optional[EvaluationLayerProfiler] = None,
) -> Tuple[
    Tuple[baseline.RelayPosition, ...],
    Tuple[baseline.RelayServiceTask, ...],
    Tuple[baseline.ScheduledRelaySortie, ...],
    Tuple[str, ...],
    float,
]:
    started = perf_counter()
    ur_started = perf_counter()
    states = cache.relay_coverage(elite_pool.candidates, demands)
    if profiler is not None:
        profiler.add("T5", perf_counter() - ur_started)
    selection_started = perf_counter()
    all_positions = {
        candidate.candidate_id: cache.relay_flight_static[candidate.candidate_id]
        for candidate in elite_pool.candidates
    }
    selected = _weighted_cost_aware_greedy(
        elite_pool.candidates, states, all_positions, context.relay_parameters
    )
    candidate_to_position: Dict[str, str] = {}
    extra_index = 0
    positions: List[baseline.RelayPosition] = []
    for candidate in selected:
        position_id = BASELINE_POSITION_IDS.get(candidate.candidate_id)
        if position_id is None:
            extra_index += 1
            position_id = f"E{extra_index:02d}"
        candidate_to_position[candidate.candidate_id] = position_id
        static = all_positions[candidate.candidate_id]
        positions.append(
            baseline.RelayPosition(
                **{**static.__dict__, "position_id": position_id}
            )
        )
    position_map = {row.position_id: row for row in positions}
    coverage_map = _coverage_map_for_selected(
        demands, states, selected, candidate_to_position
    )
    priority = tuple(candidate_to_position[row.candidate_id] for row in selected)
    assignments = baseline.assign_demands_by_priority(
        demands, coverage_map, priority
    )
    if profiler is not None:
        profiler.add("T6", perf_counter() - selection_started)
    service_started = perf_counter()
    tasks = baseline.build_service_tasks(assignments)
    plans = _cost_aware_sortie_plans(
        tasks, position_map, context.relay_parameters
    )
    if profiler is not None:
        profiler.add("T7", perf_counter() - service_started)
    schedule_timing: Dict[str, float] = {}
    try:
        scheduled = baseline.schedule_relay_sorties(
            plans,
            position_map,
            context.relay_parameters,
            timing_accumulator=schedule_timing,
        )
    finally:
        if profiler is not None:
            profiler.add("T8", schedule_timing.get("relay_uav_scheduling", 0.0))
            profiler.add("T9", schedule_timing.get("relay_component_scheduling", 0.0))
    return (
        tuple(positions),
        tasks,
        scheduled,
        tuple(row.candidate_id for row in selected),
        perf_counter() - started,
    )


def decode_problem3_solution(
    solution: Problem3Solution,
    context: PhaseAContext,
    cache: GeometryCommunicationCache,
    elite_pool: ElitePool,
    weights: Sequence[float],
    verify_exact: bool = True,
    profiler: Optional[EvaluationLayerProfiler] = None,
) -> Problem3DecodedState:
    other_started = perf_counter()
    objective_weights = validate_objective_weights(weights)
    other_elapsed = perf_counter() - other_started
    transport, trajectories = decode_transport(solution, context, profiler=profiler)
    if not transport.feasible:
        raise baseline.SchedulingConflict("运输高层解未通过问题二正式可行性检查")
    outage_started = perf_counter()
    outages = cache.direct_outages(trajectories)
    demands = core.build_outage_demands(outages)
    if profiler is not None:
        profiler.add("T4", perf_counter() - outage_started)
    positions, tasks, relay_sorties, selected_ids, relay_runtime = fast_relay_decode(
        demands, elite_pool, cache, context, profiler=profiler
    )
    position_map = {row.position_id: row for row in positions}
    verification_started = perf_counter()
    if verify_exact:
        communication_check = baseline.verify_continuous_communication(
            trajectories,
            demands,
            relay_sorties,
            position_map,
            context.communication_parameters,
            context.gateway,
            context.dem,
        )
    else:
        communication_check = baseline.CommunicationCheckResult(
            timeline=pd.DataFrame(),
            uncovered_duration_s=0.0,
            uncovered_demand_ids=tuple(),
            all_samples_available=True,
            all_relay_gateway_links_available=True,
        )
    verification_runtime = perf_counter() - verification_started

    # build_feasibility_table 的该参数仅用于旧 baseline 的20架次断言；下方会用
    # 动态运输可行性覆盖该检查，避免 ALNS 每次评价重复读取 Excel。
    other_started = perf_counter()
    transport_table = pd.DataFrame(index=range(len(trajectories)))
    feasibility = baseline.build_feasibility_table(
        positions,
        tasks,
        relay_sorties,
        context.relay_parameters,
        communication_check,
        trajectories,
        transport_table,
    )
    # baseline 检查表第一项原本专门断言固定的20架次。联合搜索允许架次数改变，
    # 因而用问题二正式 evaluator 的覆盖/硬约束结果和轨迹内部时刻一致性替换该项。
    transport_check = (
        transport.feasible
        and transport.coverage_ok
        and max((row.maximum_time_error_s for row in trajectories), default=0.0)
        <= core.TIME_TOLERANCE_S
    )
    feasibility.loc[feasibility["Check_ID"].eq(1), "Result"] = (
        "PASS" if transport_check else "FAIL"
    )
    feasibility.loc[feasibility["Check_ID"].eq(1), "Detail"] = (
        f"transport_sorties={transport.z4_sorties}; "
        f"coverage_ok={transport.coverage_ok}; "
        f"hard_violations={transport.hard_violation_count}"
    )
    if profiler is not None:
        profiler.add("T11", other_elapsed + perf_counter() - other_started)
    objective_started = perf_counter()
    relay_return = max((row.return_end_s for row in relay_sorties), default=0.0)
    relay_energy = sum(row.total_energy_kwh for row in relay_sorties)
    objectives = (
        transport.z1_weighted_tardiness,
        max(transport.z2_makespan_s, relay_return),
        transport.z3_energy_kwh + relay_energy,
        float(transport.z4_sorties + len(relay_sorties)),
    )
    normalized = tuple(
        value / scale
        for value, scale in zip(objectives, PROBLEM3_NORMALIZATION_SCALES)
    )
    objective = sum(
        weight * value for weight, value in zip(objective_weights, normalized)
    )
    feasible = bool(feasibility["Result"].eq("PASS").all())
    if profiler is not None:
        profiler.add("T10", perf_counter() - objective_started)
    state = Problem3DecodedState(
        transport_evaluation=transport,
        trajectories=trajectories,
        outages=outages,
        demands=demands,
        relay_positions=positions,
        relay_service_tasks=tasks,
        relay_sorties=relay_sorties,
        communication_check=communication_check,
        feasibility=feasibility,
        objectives=objectives,
        normalized=normalized,
        objective=objective,
        feasible=feasible,
        relay_decode_runtime_s=relay_runtime,
        full_verification_runtime_s=verification_runtime,
        selected_candidate_ids=selected_ids,
    )
    solution.decoded_state = state
    return state


def evaluate_problem3_solution(
    solution: Problem3Solution,
    context: PhaseAContext,
    cache: GeometryCommunicationCache,
    elite_pool: ElitePool,
    weights: Sequence[float],
    verify_exact: bool = False,
    profiler: Optional[EvaluationLayerProfiler] = None,
) -> Problem3EvaluationResult:
    """ALNS 安全评价入口：预期内不可行解返回 +inf，不终止搜索。"""
    try:
        decoded = decode_problem3_solution(
            solution,
            context,
            cache,
            elite_pool,
            weights,
            verify_exact=verify_exact,
            profiler=profiler,
        )
    except (baseline.SchedulingConflict, ValueError) as error:
        solution.decoded_state = None
        return Problem3EvaluationResult(
            decoded_state=None,
            feasible=False,
            objective=math.inf,
            infeasibility_reason=str(error),
        )
    if not decoded.feasible:
        return Problem3EvaluationResult(
            decoded_state=decoded,
            feasible=False,
            objective=math.inf,
            infeasibility_reason="Problem 3 feasibility checker failed",
        )
    return Problem3EvaluationResult(
        decoded_state=decoded,
        feasible=True,
        objective=decoded.objective,
    )


def _candidate_export_table(
    elite_pool: ElitePool,
) -> pd.DataFrame:
    table = elite_pool.selection_table.copy()
    candidate_map = {row.candidate_id: row for row in elite_pool.candidates}
    table["Covered_State_Count"] = table["Candidate_ID"].map(
        lambda value: len(candidate_map[str(value)].covered_state_indices)
    )
    table["Baseline_Covered_Duration_Recomputed_s"] = table["Candidate_ID"].map(
        lambda value: candidate_map[str(value)].covered_duration_s
    )
    table["Coverage_State_Ranges"] = table["Candidate_ID"].map(
        lambda value: _run_length_encode(
            candidate_map[str(value)].covered_state_indices
        )
    )
    return table


def _run_length_encode(indexes: np.ndarray) -> str:
    if len(indexes) == 0:
        return ""
    values = np.sort(np.asarray(indexes, dtype=int))
    ranges: List[str] = []
    start = previous = int(values[0])
    for value in values[1:]:
        current = int(value)
        if current == previous + 1:
            previous = current
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = current
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ";".join(ranges)


def export_phase_a_audit(
    elite_pool: ElitePool,
    cache: GeometryCommunicationCache,
    decoded: Problem3DecodedState,
    comparison: pd.DataFrame,
    runtime_s: float,
    output_file: Path = ELITE_POOL_OUTPUT_FILE,
) -> None:
    elite_candidate_ids = {row.candidate_id for row in elite_pool.candidates}
    retained_counts = {
        candidate_id: int(candidate_id in elite_candidate_ids)
        for candidate_id in BASELINE_CANDIDATE_IDS
    }
    selected_counts = {
        candidate_id: int(candidate_id in decoded.selected_candidate_ids)
        for candidate_id in BASELINE_CANDIDATE_IDS
    }
    summary = pd.DataFrame(
        [
            {
                "Phase": "A",
                "Fallback_RG_Usable_Candidates": len(elite_pool.fallback_rg_usable),
                "Elite_Candidate_Count": len(elite_pool.candidates),
                "P1_Retained": retained_counts[BASELINE_CANDIDATE_IDS[0]],
                "P2_Retained": retained_counts[BASELINE_CANDIDATE_IDS[1]],
                "P3_Retained": retained_counts[BASELINE_CANDIDATE_IDS[2]],
                "P1_Selected_By_Warm_Decoder": selected_counts[BASELINE_CANDIDATE_IDS[0]],
                "P2_Selected_By_Warm_Decoder": selected_counts[BASELINE_CANDIDATE_IDS[1]],
                "P3_Selected_By_Warm_Decoder": selected_counts[BASELINE_CANDIDATE_IDS[2]],
                "Warm_Start_Selected_Candidates": ";".join(
                    decoded.selected_candidate_ids
                ),
                "Warm_Start_Outage_Count": len(decoded.outages),
                "Warm_Start_Demand_Count": len(decoded.demands),
                "Warm_Start_Relay_Sorties": len(decoded.relay_sorties),
                "Warm_Start_Uncovered_Duration_s": (
                    decoded.communication_check.uncovered_duration_s
                ),
                "Warm_Start_Z1": decoded.objectives[0],
                "Warm_Start_Z2_s": decoded.objectives[1],
                "Warm_Start_Z3_kWh": decoded.objectives[2],
                "Warm_Start_Z4": decoded.objectives[3],
                "Warm_Start_Normalized_F": decoded.objective,
                "Feasibility_PASS_Count": int(
                    decoded.feasibility["Result"].eq("PASS").sum()
                ),
                "Feasibility_Check_Count": len(decoded.feasibility),
                "Elite_Preprocessing_Runtime_s": elite_pool.preprocessing_runtime_s,
                "Fast_Relay_Decode_Runtime_s": decoded.relay_decode_runtime_s,
                "Exact_Verification_Runtime_s": decoded.full_verification_runtime_s,
                "Phase_A_Total_Runtime_s": runtime_s,
            }
        ]
    )
    normalization = pd.DataFrame(
        {
            "Objective": ["Z1", "Z2", "Z3", "Z4"],
            "Fixed_Reference_Scale": PROBLEM3_NORMALIZATION_SCALES,
            "Dynamic_Update_Allowed": ["NO"] * 4,
        }
    )
    selection_method = pd.DataFrame(
        [
            {"Stage": 1, "Method": "filter all 18,016 RG-usable candidates"},
            {"Stage": 2, "Method": "coverage/energy/lead/max-service stratified seeds"},
            {"Stage": 3, "Method": "UTM+AGL deterministic farthest-point diversity"},
            {"Stage": 4, "Method": "compute baseline UR coverage for 300 pre-candidates"},
            {"Stage": 5, "Method": "rare-demand score and Pareto-like multi-score rerank"},
            {"Stage": 6, "Method": "retain 240 elite candidates including P1/P2/P3"},
        ]
    )
    relay_sorties = baseline._sorties_table(decoded.relay_sorties)
    position_to_candidate = {
        row.position_id: row.candidate_id for row in decoded.relay_positions
    }
    relay_sorties.insert(
        relay_sorties.columns.get_loc("Position_ID") + 1,
        "Candidate_ID",
        relay_sorties["Position_ID"].map(position_to_candidate),
    )
    output_file.parent.mkdir(parents=True, exist_ok=True)
    paths.ensure_output_path(output_file)
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        normalization.to_excel(writer, sheet_name="Normalization", index=False)
        selection_method.to_excel(writer, sheet_name="Selection_Method", index=False)
        _candidate_export_table(elite_pool).to_excel(
            writer, sheet_name="Elite_Candidates", index=False
        )
        elite_pool.fallback_rg_usable.to_excel(
            writer, sheet_name="Fallback_RG_Usable", index=False
        )
        cache.manifest().to_excel(writer, sheet_name="Cache_Manifest", index=False)
        relay_sorties.to_excel(writer, sheet_name="Warm_Start_Relay_Sorties", index=False)
        comparison.to_excel(writer, sheet_name="Baseline_Comparison", index=False)
        decoded.feasibility.to_excel(writer, sheet_name="Feasibility", index=False)
        for worksheet in writer.book.worksheets:
            worksheet.freeze_panes = "A2"
            worksheet.auto_filter.ref = worksheet.dimensions
            for column in worksheet.columns:
                width = min(
                    max(
                        max(
                            (len(str(cell.value)) if cell.value is not None else 0)
                            for cell in column[:500]
                        )
                        + 2,
                        12,
                    ),
                    45,
                )
                worksheet.column_dimensions[column[0].column_letter].width = width
                for cell in column[1:]:
                    if isinstance(cell.value, float):
                        cell.number_format = "0.000000000"


def compare_with_baseline(decoded: Problem3DecodedState) -> pd.DataFrame:
    baseline_objective = pd.read_excel(paths.require_file(BASELINE_FILE), sheet_name="Objective").iloc[0]
    labels = ("Z1", "Z2", "Z3", "Z4")
    baseline_values = (
        float(baseline_objective["Z1_Weighted_Tardiness"]),
        float(baseline_objective["Z2_Joint_Makespan_s"]),
        float(baseline_objective["Z3_Total_Energy_kWh"]),
        float(baseline_objective["Z4_Total_Sorties"]),
    )
    rows = []
    for label, reference, reconstructed in zip(
        labels, baseline_values, decoded.objectives
    ):
        difference = reconstructed - reference
        tolerance = 1e-6 if label in {"Z1", "Z4"} else 0.05
        rows.append(
            {
                "Metric": label,
                "Baseline": reference,
                "Reconstructed": reconstructed,
                "Difference": difference,
                "Tolerance": tolerance,
                "Result": "PASS" if abs(difference) <= tolerance else "FAIL",
            }
        )
    return pd.DataFrame(rows)


def run_phase_a(
    weights: Sequence[float] = (0.25, 0.25, 0.25, 0.25),
    worker_count: int = DEFAULT_COVERAGE_WORKERS,
) -> Problem3DecodedState:
    """构建 elite pool 并完成 baseline warm-start 全链路复核。"""
    started = perf_counter()
    context = load_phase_a_context()
    cache = GeometryCommunicationCache(context)
    solution = load_baseline_warm_start()
    transport, trajectories = decode_transport(solution, context)
    if not transport.feasible:
        raise RuntimeError("baseline warm start运输解码失败")
    outages = cache.direct_outages(trajectories)
    demands = core.build_outage_demands(outages)
    print(
        f"Warm transport: sorties={len(transport.scheduled)}, "
        f"outages={len(outages)}, demands={len(demands)}, "
        f"duration={sum(row.duration_weight_s for row in demands):.9f}s",
        flush=True,
    )
    elite_pool = build_elite_pool(
        context, cache, demands, worker_count=worker_count
    )
    print(
        f"Elite pool: {len(elite_pool.candidates)} / "
        f"{len(elite_pool.fallback_rg_usable)} RG-usable, "
        f"preprocess={elite_pool.preprocessing_runtime_s:.3f}s",
        flush=True,
    )
    decoded = decode_problem3_solution(
        solution,
        context,
        cache,
        elite_pool,
        weights,
        verify_exact=True,
    )
    comparison = compare_with_baseline(decoded)
    if not comparison["Result"].eq("PASS").all():
        print(comparison.to_string(index=False), flush=True)
        raise RuntimeError("warm-start目标与baseline差异明显，停止Phase A")
    if not decoded.feasibility["Result"].eq("PASS").all():
        print(decoded.feasibility.to_string(index=False), flush=True)
        raise RuntimeError("warm-start未通过13/13可行性检查")
    runtime = perf_counter() - started
    export_phase_a_audit(
        elite_pool, cache, decoded, comparison, runtime, ELITE_POOL_OUTPUT_FILE
    )
    print(
        f"Selected relay candidates={decoded.selected_candidate_ids}; "
        f"relay sorties={len(decoded.relay_sorties)}",
        flush=True,
    )
    print(
        "Objectives: "
        + ", ".join(
            f"{name}={value:.9f}"
            for name, value in zip(("Z1", "Z2", "Z3", "Z4"), decoded.objectives)
        ),
        flush=True,
    )
    print(
        f"Fast relay decode={decoded.relay_decode_runtime_s:.6f}s; "
        f"exact verify={decoded.full_verification_runtime_s:.6f}s; "
        f"feasibility={len(decoded.feasibility)}/{len(decoded.feasibility)} PASS; "
        f"total={runtime:.6f}s",
        flush=True,
    )
    print(f"Elite audit={ELITE_POOL_OUTPUT_FILE}", flush=True)
    return decoded


PHASE_B_OUTPUT_FILE = paths.output_file("problem3", "problem3_alns_final.xlsx")
PHASE_B_LOG_FILE = paths.output_file("problem3", "problem3_alns_final.log")
LAZY_CACHE_TEST_OUTPUT_FILE = (
    paths.output_file("problem3", "problem3_alns_lazy_cache_10iter.xlsx")
)
LAZY_CACHE_TEST_LOG_FILE = (
    paths.output_file("problem3", "problem3_alns_lazy_cache_10iter.log")
)


@dataclass(frozen=True)
class PhaseBConfig:
    """Phase B 联合 ALNS 的集中参数。"""

    iterations: int = 500
    random_seed: int = 20260925
    objective_weights: Tuple[float, float, float, float] = (
        0.55, 0.15, 0.15, 0.15
    )
    destroy_fraction_min: float = 0.08
    destroy_fraction_max: float = 0.20
    communication_burden_weights: Tuple[float, float, float, float] = (
        0.25, 0.25, 0.25, 0.25
    )
    # 性能审计后的第一轮调试设置：每轮 LEVEL 1 调用控制在1--5次。
    communication_top_k: int = 2
    local_top_k: int = 2
    max_local_trials: int = 3
    local_search_probability: float = 0.15
    sigma_local_probability: float = 0.30
    initial_worse_fraction: float = 0.05
    initial_accept_probability: float = 0.50
    cooling_rate: float = 0.98
    sigma_best: float = 10.0
    sigma_improve: float = 5.0
    sigma_accept: float = 1.0
    sigma_reject: float = 0.0
    reaction_factor: float = 0.20
    segment_length: int = 20
    minimum_operator_weight: float = 0.05
    log_interval: int = 10


@dataclass
class AdaptiveOperatorStat:
    category: str
    name: str
    weight: float = 1.0
    calls: int = 0
    feasible: int = 0
    accepted: int = 0
    improving: int = 0
    new_best: int = 0
    successes: int = 0
    segment_calls: int = 0
    segment_score: float = 0.0


@dataclass(frozen=True)
class CachedEvaluation:
    result: Problem3EvaluationResult
    elapsed_s: float
    cache_hit: bool


def canonical_solution_key(solution: Problem3Solution) -> Tuple[Tuple[object, ...], ...]:
    """包含 sigma 的高层解规范键；每个架次内部箱集合排序。"""
    return tuple(
        (
            tuple(sorted(sortie.box_ids)),
            tuple(sortie.route),
            sortie.uav_type,
        )
        for sortie in solution.transport_sorties
    )


class SolutionEvaluationCache:
    """避免对相同运输高层解重复构建轨迹、通信需求和中继调度。"""

    def __init__(
        self,
        context: PhaseAContext,
        geometry_cache: GeometryCommunicationCache,
        elite_pool: ElitePool,
        weights: Sequence[float],
        layer_profiler: Optional[EvaluationLayerProfiler] = None,
    ) -> None:
        self.context = context
        self.geometry_cache = geometry_cache
        self.elite_pool = elite_pool
        self.weights = validate_objective_weights(weights)
        self.layer_profiler = layer_profiler or EvaluationLayerProfiler()
        self.values: Dict[
            Tuple[Tuple[object, ...], ...], Problem3EvaluationResult
        ] = {}
        self.request_count = 0
        self.hit_count = 0
        self.miss_count = 0
        self.miss_times_s: List[float] = []
        self.top_k_full_calls = 0
        self.infeasibility_counts: Counter[str] = Counter()
        self.caller_profiles: Dict[str, CallerProfile] = {
            name: CallerProfile() for name in EVALUATOR_CALLERS
        }

    def seed(
        self, solution: Problem3Solution, result: Problem3EvaluationResult
    ) -> None:
        self.values[canonical_solution_key(solution)] = result

    def evaluate(
        self,
        solution: Problem3Solution,
        caller: str = "OTHER",
    ) -> CachedEvaluation:
        request_started = perf_counter()
        caller = caller if caller in self.caller_profiles else "OTHER"
        self.request_count += 1
        key = canonical_solution_key(solution)
        cached = self.values.get(key)
        if cached is not None:
            self.hit_count += 1
            if cached.decoded_state is not None:
                solution.decoded_state = cached.decoded_state
            elapsed = perf_counter() - request_started
            profile = self.caller_profiles[caller]
            profile.calls += 1
            profile.total_s += elapsed
            profile.times_s.append(elapsed)
            profile.cache_hits += 1
            return CachedEvaluation(cached, elapsed, True)
        started = perf_counter()
        result = evaluate_problem3_solution(
            solution,
            self.context,
            self.geometry_cache,
            self.elite_pool,
            self.weights,
            verify_exact=False,
            profiler=self.layer_profiler,
        )
        elapsed = perf_counter() - started
        self.miss_count += 1
        self.miss_times_s.append(elapsed)
        self.values[key] = result
        if not result.feasible:
            self.infeasibility_counts[
                categorize_infeasibility(solution, result, self.context)
            ] += 1
        profile = self.caller_profiles[caller]
        profile.calls += 1
        profile.total_s += elapsed
        profile.times_s.append(elapsed)
        profile.cache_misses += 1
        return CachedEvaluation(result, elapsed, False)

    def caller_table(self) -> pd.DataFrame:
        rows = []
        for caller in EVALUATOR_CALLERS:
            profile = self.caller_profiles[caller]
            rows.append(
                {
                    "Caller": caller,
                    "Call_Count": profile.calls,
                    "Total_s": profile.total_s,
                    "Mean_s": (
                        profile.total_s / profile.calls if profile.calls else 0.0
                    ),
                    "Median_s": (
                        float(np.median(profile.times_s))
                        if profile.times_s else 0.0
                    ),
                    "Cache_Hits": profile.cache_hits,
                    "Cache_Misses": profile.cache_misses,
                }
            )
        return pd.DataFrame(rows)


def categorize_infeasibility(
    solution: Problem3Solution,
    result: Problem3EvaluationResult,
    context: PhaseAContext,
) -> str:
    """把 safe evaluator 的失败映射到约定的九类统计口径。"""
    data = context.transport_data
    assigned = [
        box_id
        for sortie in solution.transport_sorties
        for box_id in sortie.box_ids
    ]
    if set(assigned) != set(data.boxes) or any(
        count != 1 for count in Counter(assigned).values()
    ):
        return "other"
    for sortie in solution.transport_sorties:
        parameters = data.uav_types.get(sortie.uav_type)
        if parameters is None:
            return "transport_capacity"
        weight = sum(data.boxes[value].weight_kg for value in sortie.box_ids)
        volume = sum(data.boxes[value].volume_m3 for value in sortie.box_ids)
        if (
            weight > parameters.payload_limit_kg + p2_core.TOLERANCE
            or volume > parameters.volume_limit_m3 + p2_core.TOLERANCE
        ):
            return "transport_capacity"
        try:
            metrics = p2_core.get_sortie_metrics(sortie, data)
        except (KeyError, ValueError):
            return "other"
        if metrics.energy_kwh > parameters.energy_limit_kwh + p2_core.TOLERANCE:
            return "transport_energy"
    transport = p2_improved.evaluate_solution(
        solution.transport_sorties, data, require_complete=True
    )
    if transport.hard_violation_count:
        return "hard_deadline"
    reason = result.infeasibility_reason.lower()
    if "覆盖" in reason or "coverage" in reason or "需求" in reason:
        return "relay_coverage"
    if "能耗" in reason or "energy" in reason or "2.56" in reason:
        return "relay_energy"
    if "r01" in reason or "r02" in reason or "中继无人机" in reason:
        return "relay_uav_resource"
    if "组件" in reason or "component" in reason or "充电" in reason:
        return "relay_component_resource"
    if "运输" in reason or not transport.feasible:
        return "transport_resource"
    return "other"


def _roulette_choice(
    stats: Mapping[str, AdaptiveOperatorStat], rng: random.Random
) -> str:
    names = tuple(stats)
    weights = [max(stats[name].weight, NUMERIC_TOLERANCE) for name in names]
    return rng.choices(names, weights=weights, k=1)[0]


def _update_adaptive_weights(
    stats: Mapping[str, AdaptiveOperatorStat], config: PhaseBConfig
) -> None:
    for stat in stats.values():
        if stat.segment_calls:
            average = stat.segment_score / stat.segment_calls
            stat.weight = max(
                config.minimum_operator_weight,
                (1.0 - config.reaction_factor) * stat.weight
                + config.reaction_factor * average,
            )
        stat.segment_calls = 0
        stat.segment_score = 0.0


def route_segment_removal(
    solution: Sequence[p2_core.Sortie],
    evaluation: p2_core.Evaluation,
    data: p2_core.ProblemData,
    rng: random.Random,
) -> Tuple[List[p2_core.Sortie], List[str]]:
    """移除一个随机架次中连续服务区段对应的全部货箱。"""
    target_count = p2_improved.removal_count(data, rng)
    candidates = [row for row in solution if row.route]
    target = rng.choice(candidates)
    left = rng.randrange(len(target.route))
    right = rng.randrange(left + 1, len(target.route) + 1)
    zones = set(target.route[left:right])
    removed = [
        box_id
        for box_id in target.box_ids
        if data.boxes[box_id].service_zone in zones
    ]
    remaining = [
        box_id
        for sortie in solution
        for box_id in sortie.box_ids
        if box_id not in removed
    ]
    if len(removed) < target_count and remaining:
        removed.extend(
            rng.sample(remaining, min(target_count - len(removed), len(remaining)))
        )
    return p2_improved.remove_boxes(solution, removed, data), removed


def _normalize_by_max(values: Mapping[str, float]) -> Dict[str, float]:
    scale = max(values.values(), default=0.0)
    if scale <= NUMERIC_TOLERANCE:
        return {key: 0.0 for key in values}
    return {key: value / scale for key, value in values.items()}


def communication_hotspot_removal(
    solution: Problem3Solution,
    evaluation: Problem3EvaluationResult,
    context: PhaseAContext,
    cache: GeometryCommunicationCache,
    config: PhaseBConfig,
    rng: random.Random,
) -> Tuple[List[p2_core.Sortie], List[str]]:
    """按直连中断、稀缺覆盖和中继能耗贡献移除高通信负担架次。"""
    if evaluation.decoded_state is None:
        return p2_improved.random_removal(
            solution.transport_sorties,
            p2_improved.evaluate_solution(
                solution.transport_sorties, context.transport_data, True
            ),
            context.transport_data,
            rng,
        )
    decoded = evaluation.decoded_state
    ids = [row.sortie_id for row in decoded.transport_evaluation.scheduled]
    outage = {value: 0.0 for value in ids}
    relay_required = {value: 0.0 for value in ids}
    rare = {value: 0.0 for value in ids}
    energy = {value: 0.0 for value in ids}
    for demand in decoded.demands:
        outage[demand.sortie_id] += demand.duration_weight_s
        relay_required[demand.sortie_id] += demand.duration_weight_s
        key = cache._point_key(demand.point)
        count = len(cache.ur_state_covering_candidates.get(key, ()))
        rare[demand.sortie_id] += demand.duration_weight_s / max(count, 1)
    for relay_sortie in decoded.relay_sorties:
        covered = relay_sortie.covered_transport_sorties
        if covered:
            share = relay_sortie.total_energy_kwh / len(covered)
            for sortie_id in covered:
                energy[sortie_id] = energy.get(sortie_id, 0.0) + share
    components = tuple(
        _normalize_by_max(values)
        for values in (outage, relay_required, rare, energy)
    )
    burdens = {
        sortie_id: sum(
            weight * component.get(sortie_id, 0.0)
            for weight, component in zip(
                config.communication_burden_weights, components
            )
        )
        for sortie_id in ids
    }
    target_count = p2_improved.removal_count(context.transport_data, rng)
    removed: List[str] = []
    schedule_map = {
        row.sortie_id: row.sortie
        for row in decoded.transport_evaluation.scheduled
    }
    for sortie_id in sorted(ids, key=lambda value: (-burdens[value], value)):
        removed.extend(schedule_map[sortie_id].box_ids)
        if len(removed) >= target_count:
            break
    return (
        p2_improved.remove_boxes(
            solution.transport_sorties, removed, context.transport_data
        ),
        removed,
    )


@dataclass(frozen=True)
class CheapCommunicationProxy:
    """LEVEL 0 仅含预计算静态距离风险，不访问轨迹、DEM或中继 decoder。"""

    direct_risk_by_zone: Mapping[str, float]
    relay_proximity_risk_by_zone: Mapping[str, float]


def build_cheap_communication_proxy(
    context: PhaseAContext,
    elite_pool: ElitePool,
) -> CheapCommunicationProxy:
    direct: Dict[str, float] = {}
    proximity: Dict[str, float] = {}
    for node_id, node in context.nodes.items():
        if node_id == p2_core.DEPOT:
            continue
        endpoint = communication.Point3D(
            node.longitude_deg, node.latitude_deg, node.operation_altitude_m
        )
        zone = p2_core.to_segment_node(node_id)
        direct[zone] = communication.horizontal_distance_m(
            context.gateway.point, endpoint
        ) / 50_000.0
        proximity[zone] = min(
            communication.horizontal_distance_m(
                endpoint,
                communication.Point3D(
                    candidate.longitude_deg,
                    candidate.latitude_deg,
                    candidate.absolute_altitude_m,
                ),
            )
            for candidate in elite_pool.candidates
        ) / 25_000.0
    return CheapCommunicationProxy(direct, proximity)


def _cheap_insertion_candidates(
    solution: Sequence[p2_core.Sortie],
    box_id: str,
    data: p2_core.ProblemData,
) -> List[List[p2_core.Sortie]]:
    """只做单架次局部可行性生成；不运行问题二/问题三资源 decoder。"""
    result: Dict[Tuple[Tuple[object, ...], ...], List[p2_core.Sortie]] = {}
    for index, sortie in enumerate(solution):
        box_ids = tuple((*sortie.box_ids, box_id))
        for uav_type in p2_core.UAV_TYPES:
            rebuilt = p2_core.optimize_route(box_ids, uav_type, data)
            if rebuilt is None:
                continue
            candidate = list(solution)
            candidate[index] = rebuilt
            key = canonical_solution_key(Problem3Solution(tuple(candidate)))
            result[key] = candidate
    for uav_type in p2_core.UAV_TYPES:
        rebuilt = p2_core.optimize_route((box_id,), uav_type, data)
        if rebuilt is None:
            continue
        candidate = p2_improved.insert_by_urgency(solution, rebuilt, data)
        key = canonical_solution_key(Problem3Solution(tuple(candidate)))
        result[key] = candidate
    return list(result.values())


def cheap_joint_score(
    sorties: Sequence[p2_core.Sortie],
    context: PhaseAContext,
    proxy: CheapCommunicationProxy,
) -> float:
    """Repair/local-search 预筛分，不执行轨迹、UR或中继调度。"""
    metrics = [
        p2_core.get_sortie_metrics(row, context.transport_data) for row in sorties
    ]
    total_duration = sum(row.duration_s for row in metrics)
    total_energy = sum(row.energy_kwh for row in metrics)
    relative_tardiness = 0.0
    hard_slacks: List[float] = []
    for sortie, sortie_metrics in zip(sorties, metrics):
        for box_id, delivery_s in sortie_metrics.delivery_relative_s:
            box = context.transport_data.boxes[box_id]
            relative_tardiness += box.emergency_priority * max(
                0.0, delivery_s - box.expected_time_s
            )
            hard_slacks.extend(
                deadline - delivery_s for deadline in box.hard_deadlines
            )
    transport_proxy = 0.25 * (
        relative_tardiness / PROBLEM3_NORMALIZATION_SCALES[0]
        + max((row.duration_s for row in metrics), default=0.0)
        / PROBLEM3_NORMALIZATION_SCALES[1]
        + total_energy / PROBLEM3_NORMALIZATION_SCALES[2]
        + len(sorties) / PROBLEM3_NORMALIZATION_SCALES[3]
    )
    direct_risk = 0.0
    relay_risk = 0.0
    for sortie in sorties:
        direct_risk += max(
            (proxy.direct_risk_by_zone.get(zone, 0.0) for zone in sortie.route),
            default=0.0,
        )
        relay_risk += max(
            (
                proxy.relay_proximity_risk_by_zone.get(zone, 0.0)
                for zone in sortie.route
            ),
            default=0.0,
        )
    count = max(len(sorties), 1)
    direct_risk /= count
    relay_risk /= count
    minimum_slack = min(hard_slacks, default=3600.0)
    slack_risk = max(0.0, 600.0 - minimum_slack) / 600.0
    return (
        0.65 * transport_proxy
        + 0.10 * total_duration / PROBLEM3_NORMALIZATION_SCALES[1]
        + 0.15 * direct_risk
        + 0.05 * relay_risk
        + 0.05 * slack_risk
    )


def communication_aware_repair(
    partial: Sequence[p2_core.Sortie],
    removed: Sequence[str],
    context: PhaseAContext,
    elite_pool: ElitePool,
    cheap_proxy: CheapCommunicationProxy,
    evaluator: SolutionEvaluationCache,
    config: PhaseBConfig,
    performance: Optional[SearchPerformanceCounters] = None,
) -> Optional[CachedEvaluation]:
    """Top-K beam 便宜预筛；仅对完整的 Top-K 解调用 Problem 3 evaluator。"""
    pending = sorted(
        removed,
        key=lambda box_id: (
            min(
                context.transport_data.boxes[box_id].hard_deadlines,
                default=math.inf,
            ),
            context.transport_data.boxes[box_id].expected_time_s,
            -context.transport_data.boxes[box_id].emergency_priority,
            box_id,
        ),
    )
    beams: List[List[p2_core.Sortie]] = [list(partial)]
    if performance is not None:
        performance.repaired_box_counts.append(len(pending))
    for box_id in pending:
        expanded: Dict[Tuple[Tuple[object, ...], ...], Tuple[float, List[p2_core.Sortie]]] = {}
        insertion_count = 0
        for beam in beams:
            insertion_candidates = _cheap_insertion_candidates(
                beam, box_id, context.transport_data
            )
            insertion_count += len(insertion_candidates)
            for candidate in insertion_candidates:
                wrapped = Problem3Solution(tuple(candidate))
                key = canonical_solution_key(wrapped)
                score_started = perf_counter()
                score = cheap_joint_score(candidate, context, cheap_proxy)
                if performance is not None:
                    performance.level0_score_times_s.append(
                        perf_counter() - score_started
                    )
                previous = expanded.get(key)
                if previous is None or score < previous[0]:
                    expanded[key] = (score, candidate)
        if performance is not None:
            performance.insertion_candidates_per_box.append(insertion_count)
        if not expanded:
            return None
        beams = [
            value[1]
            for value in sorted(
                expanded.values(), key=lambda item: (item[0], str(item[1]))
            )[: config.communication_top_k]
        ]
    full_candidates = [
        Problem3Solution(tuple(candidate))
        for candidate in beams[: config.communication_top_k]
    ]
    prefill_s = _prefill_top_k_geometry(
        full_candidates, context, evaluator.geometry_cache, elite_pool
    )
    if performance is not None:
        performance.geometry_prefill_s.append(prefill_s)
    evaluated: List[CachedEvaluation] = []
    full_calls = 0
    for candidate in full_candidates:
        evaluator.top_k_full_calls += 1
        full_calls += 1
        record = evaluator.evaluate(candidate, caller="COMMUNICATION_AWARE_REPAIR")
        if record.result.feasible:
            evaluated.append(record)
    if performance is not None:
        performance.repair_full_calls.append(full_calls)
    if not evaluated:
        return None
    return min(evaluated, key=lambda row: row.result.objective)


def _prefill_top_k_geometry(
    solutions: Sequence[Problem3Solution],
    context: PhaseAContext,
    cache: GeometryCommunicationCache,
    elite_pool: ElitePool,
) -> float:
    """合并 Top-K 新空间状态，只启动一次完全相同口径的精确 UR 批处理。"""
    started = perf_counter()
    # lazy cache必须由正式LEVEL 1按航段触发；不能在Top-K评估前批量展开。
    if cache.lazy_segment_cache is not None:
        return perf_counter() - started
    all_demands: List[core.CommunicationDemand] = []
    for solution in solutions:
        transport, trajectories = decode_transport(solution, context)
        if not transport.feasible:
            continue
        outages = cache.direct_outages(trajectories)
        all_demands.extend(core.build_outage_demands(outages))
    if all_demands:
        cache.relay_coverage(elite_pool.candidates, all_demands)
    return perf_counter() - started


def _box_relocate_candidate(
    solution: Sequence[p2_core.Sortie],
    data: p2_core.ProblemData,
    rng: random.Random,
) -> Optional[List[p2_core.Sortie]]:
    if len(solution) < 2:
        return None
    source_index, target_index = rng.sample(range(len(solution)), 2)
    source = solution[source_index]
    box_id = rng.choice(source.box_ids)
    source_boxes = tuple(value for value in source.box_ids if value != box_id)
    target_boxes = tuple((*solution[target_index].box_ids, box_id))
    rebuilt_target = p2_core.optimize_route(
        target_boxes, solution[target_index].uav_type, data
    )
    if rebuilt_target is None:
        return None
    candidate = list(solution)
    candidate[target_index] = rebuilt_target
    if source_boxes:
        rebuilt_source = p2_core.optimize_route(source_boxes, source.uav_type, data)
        if rebuilt_source is None:
            return None
        candidate[source_index] = rebuilt_source
    else:
        candidate.pop(source_index)
    return candidate


def _box_swap_candidate(
    solution: Sequence[p2_core.Sortie],
    data: p2_core.ProblemData,
    rng: random.Random,
) -> Optional[List[p2_core.Sortie]]:
    if len(solution) < 2:
        return None
    first, second = rng.sample(range(len(solution)), 2)
    left = rng.choice(solution[first].box_ids)
    right = rng.choice(solution[second].box_ids)
    left_boxes = tuple(right if value == left else value for value in solution[first].box_ids)
    right_boxes = tuple(left if value == right else value for value in solution[second].box_ids)
    rebuilt_left = p2_core.optimize_route(
        left_boxes, solution[first].uav_type, data
    )
    rebuilt_right = p2_core.optimize_route(
        right_boxes, solution[second].uav_type, data
    )
    if rebuilt_left is None or rebuilt_right is None:
        return None
    candidate = list(solution)
    candidate[first] = rebuilt_left
    candidate[second] = rebuilt_right
    return candidate


def _route_2opt_candidate(
    solution: Sequence[p2_core.Sortie],
    data: p2_core.ProblemData,
    rng: random.Random,
) -> Optional[List[p2_core.Sortie]]:
    choices = [index for index, row in enumerate(solution) if len(row.route) >= 3]
    if not choices:
        return None
    index = rng.choice(choices)
    sortie = solution[index]
    left = rng.randrange(0, len(sortie.route) - 1)
    right = rng.randrange(left + 2, len(sortie.route) + 1)
    route = (
        sortie.route[:left]
        + tuple(reversed(sortie.route[left:right]))
        + sortie.route[right:]
    )
    rebuilt = p2_core.Sortie(sortie.box_ids, route, sortie.uav_type)
    if not p2_core.check_sortie_feasibility(rebuilt, data):
        return None
    candidate = list(solution)
    candidate[index] = rebuilt
    return candidate


def _uav_type_candidate(
    solution: Sequence[p2_core.Sortie],
    data: p2_core.ProblemData,
    rng: random.Random,
) -> Optional[List[p2_core.Sortie]]:
    index = rng.randrange(len(solution))
    sortie = solution[index]
    alternatives = [value for value in p2_core.UAV_TYPES if value != sortie.uav_type]
    rng.shuffle(alternatives)
    for uav_type in alternatives:
        rebuilt = p2_core.optimize_route(sortie.box_ids, uav_type, data)
        if rebuilt is not None:
            candidate = list(solution)
            candidate[index] = rebuilt
            return candidate
    return None


def limited_local_search(
    solution: Problem3Solution,
    current: CachedEvaluation,
    context: PhaseAContext,
    elite_pool: ElitePool,
    cheap_proxy: CheapCommunicationProxy,
    evaluator: SolutionEvaluationCache,
    config: PhaseBConfig,
    rng: random.Random,
    performance: Optional[SearchPerformanceCounters] = None,
) -> Tuple[Problem3Solution, CachedEvaluation, str]:
    """最多 MAX_LOCAL_TRIALS 个随机邻居，经 cheap score 后只完整评价 Top-K。"""
    local_started = perf_counter()
    generators = {
        "box_relocate": _box_relocate_candidate,
        "box_swap": _box_swap_candidate,
        "route_2opt": _route_2opt_candidate,
        "UAV_type_change": _uav_type_candidate,
    }
    candidates: Dict[
        Tuple[Tuple[object, ...], ...], Tuple[float, str, Problem3Solution]
    ] = {}
    for _ in range(config.max_local_trials):
        name = rng.choice(tuple(generators))
        candidate_sorties = generators[name](
            solution.transport_sorties, context.transport_data, rng
        )
        if candidate_sorties is None:
            continue
        candidate = Problem3Solution(tuple(candidate_sorties))
        key = canonical_solution_key(candidate)
        if key == canonical_solution_key(solution):
            continue
        score_started = perf_counter()
        score = cheap_joint_score(candidate_sorties, context, cheap_proxy)
        if performance is not None:
            performance.level0_score_times_s.append(
                perf_counter() - score_started
            )
        if math.isfinite(score):
            candidates[key] = (score, name, candidate)
    best_solution = solution
    best_record = current
    best_name = "none"
    screened = sorted(candidates.values(), key=lambda row: row[0])[
        : config.local_top_k
    ]
    prefill_s = _prefill_top_k_geometry(
        [row[2] for row in screened], context, evaluator.geometry_cache, elite_pool
    )
    if performance is not None and screened:
        performance.geometry_prefill_s.append(prefill_s)
    caller_by_name = {
        "box_relocate": "LOCAL_RELOCATE",
        "box_swap": "LOCAL_SWAP",
        "route_2opt": "LOCAL_2OPT",
        "UAV_type_change": "LOCAL_UAV_TYPE",
    }
    for _, name, candidate in screened:
        evaluator.top_k_full_calls += 1
        record = evaluator.evaluate(candidate, caller=caller_by_name[name])
        if record.result.feasible and (
            record.result.objective
            < best_record.result.objective - OBJECTIVE_COMPARISON_TOLERANCE
        ):
            best_solution, best_record, best_name = candidate, record, name
    if performance is not None:
        performance.local_trials.append(config.max_local_trials)
        performance.local_full_calls.append(len(screened))
        performance.local_elapsed_s.append(perf_counter() - local_started)
        if best_name != "none":
            performance.local_improvements += 1
    return best_solution, best_record, best_name


def sigma_local_move(
    solution: Problem3Solution,
    current: CachedEvaluation,
    evaluator: SolutionEvaluationCache,
    rng: random.Random,
) -> Tuple[Problem3Solution, CachedEvaluation, str]:
    if len(solution.transport_sorties) < 2:
        return solution, current, "none"
    rows = list(solution.transport_sorties)
    name = rng.choice(("sigma_swap", "sigma_insert"))
    first, second = rng.sample(range(len(rows)), 2)
    if name == "sigma_swap":
        rows[first], rows[second] = rows[second], rows[first]
    else:
        item = rows.pop(first)
        rows.insert(second, item)
    candidate = Problem3Solution(tuple(rows))
    record = evaluator.evaluate(candidate, caller="SIGMA_LOCAL")
    if record.result.feasible and (
        record.result.objective
        < current.result.objective - OBJECTIVE_COMPARISON_TOLERANCE
    ):
        return candidate, record, name
    return solution, current, name


def _transport_sorties_table(
    solution: Problem3Solution, decoded: Problem3DecodedState
) -> pd.DataFrame:
    sigma = {sortie: index for index, sortie in enumerate(solution.transport_sorties, 1)}
    rows = []
    for scheduled in decoded.transport_evaluation.scheduled:
        metrics = scheduled.metrics
        rows.append(
            {
                "Sortie_ID": scheduled.sortie_id,
                "Schedule_Order": sigma[scheduled.sortie],
                "UAV_ID": scheduled.uav_id,
                "Battery_ID": scheduled.battery_id,
                "UAV_Type": scheduled.sortie.uav_type,
                "Box_IDs": ";".join(scheduled.sortie.box_ids),
                "Route": "O01->" + "->".join(scheduled.sortie.route) + "->O01",
                "Start_Time_s": scheduled.start_s,
                "Takeoff_Time_s": scheduled.departure_s,
                "Return_Time_s": scheduled.completion_s,
                "Duration_s": metrics.duration_s,
                "Energy_kWh": metrics.energy_kwh,
                "Return_SOC": metrics.return_soc,
                "Charge_End_s": scheduled.charge_end_s,
            }
        )
    return pd.DataFrame(rows).sort_values("Schedule_Order")


def _operator_statistics_table(
    destroy_stats: Mapping[str, AdaptiveOperatorStat],
    repair_stats: Mapping[str, AdaptiveOperatorStat],
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Category": row.category,
                "Operator": row.name,
                "Calls": row.calls,
                "Feasible": row.feasible,
                "Accepted": row.accepted,
                "Improving": row.improving,
                "New_Best": row.new_best,
                "Successes": row.successes,
                "Final_Weight": row.weight,
            }
            for row in (*destroy_stats.values(), *repair_stats.values())
        ]
    )


def _evaluation_statistics_table(evaluator: SolutionEvaluationCache) -> pd.DataFrame:
    values = np.asarray(evaluator.miss_times_s, dtype=float)
    return pd.DataFrame(
        [
            {
                "Evaluation_Requests": evaluator.request_count,
                "Cache_Hits": evaluator.hit_count,
                "Cache_Misses": evaluator.miss_count,
                "Cache_Hit_Rate": evaluator.hit_count / max(evaluator.request_count, 1),
                "Fast_Evaluator_Mean_s": float(values.mean()) if len(values) else 0.0,
                "Fast_Evaluator_Median_s": float(np.median(values)) if len(values) else 0.0,
                "Fast_Evaluator_P95_s": float(np.percentile(values, 95)) if len(values) else 0.0,
                "TopK_Full_Evaluator_Calls": evaluator.top_k_full_calls,
            }
        ]
    )


def _phase_b_log(message: str, reset: bool = False) -> None:
    paths.ensure_output_path(PHASE_B_LOG_FILE)
    PHASE_B_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with PHASE_B_LOG_FILE.open("w" if reset else "a", encoding="utf-8") as stream:
        stream.write(message + "\n")


def run_phase_b_debug(config: PhaseBConfig = PhaseBConfig()) -> Problem3DecodedState:
    """运行且只运行一次100迭代正式Phase B，并最终执行LEVEL 2复核。"""
    started = perf_counter()
    weights = validate_objective_weights(config.objective_weights)
    rng = random.Random(config.random_seed)
    _phase_b_log(
        f"Problem 3 Phase B formal start; iterations={config.iterations}; "
        f"seed={config.random_seed}; weights={weights}; "
        f"objective_comparison_tolerance={OBJECTIVE_COMPARISON_TOLERANCE}",
        reset=True,
    )
    p2_improved.CONFIG = replace(
        p2_improved.CONFIG,
        random_seed=config.random_seed,
        destroy_fraction_min=config.destroy_fraction_min,
        destroy_fraction_max=config.destroy_fraction_max,
        objective_weights=weights,
    )
    context = load_phase_a_context()
    geometry_cache = GeometryCommunicationCache(context)
    warm_solution = load_baseline_warm_start()
    warm_transport, warm_trajectories = decode_transport(warm_solution, context)
    if not warm_transport.feasible:
        raise RuntimeError("warm-start运输解码失败")
    warm_demands, _ = relay.load_step3_demands()
    elite_pool = load_saved_elite_pool(
        context, geometry_cache, warm_demands, ELITE_POOL_FILE
    )
    lazy_cache = attach_lazy_persistent_segment_cache(
        context, geometry_cache, elite_pool
    )
    initial_cached_segments = len(lazy_cache.cache.segment_profiles)
    segment_counter_start = lazy_cache.snapshot()
    print(
        f"Objective comparison tolerance="
        f"{OBJECTIVE_COMPARISON_TOLERANCE:.1e}; "
        f"segment cache signature_ok="
        f"{lazy_cache.cache.signature == lazy_cache.expected_signature}; "
        f"cached={initial_cached_segments}/"
        f"{lazy_cache.cache.actual_directed_segment_count}",
        flush=True,
    )
    cheap_proxy = build_cheap_communication_proxy(context, elite_pool)

    print("Phase B warm-start standard audit...", flush=True)
    warm_started = perf_counter()
    warm_result = evaluate_problem3_solution(
        warm_solution,
        context,
        geometry_cache,
        elite_pool,
        weights,
        verify_exact=True,
    )
    warm_audit_s = perf_counter() - warm_started
    if not warm_result.feasible or warm_result.decoded_state is None:
        raise RuntimeError(
            "warm-start未通过标准核查: " + warm_result.infeasibility_reason
        )
    warm_decoded = warm_result.decoded_state
    expected = FORMAL_WARM_OBJECTIVES
    if any(
        abs(value - reference) > (0.05 if index in (1, 2) else 1e-6)
        for index, (value, reference) in enumerate(
            zip(warm_decoded.objectives, expected)
        )
    ):
        raise RuntimeError(
            f"warm-start目标口径不一致: {warm_decoded.objectives}"
        )
    if warm_decoded.communication_check.uncovered_duration_s > core.TIME_TOLERANCE_S:
        raise RuntimeError("warm-start标准通信核查存在未覆盖时长")

    layer_profiler = EvaluationLayerProfiler()
    evaluator = SolutionEvaluationCache(
        context, geometry_cache, elite_pool, weights, layer_profiler
    )
    search_performance = SearchPerformanceCounters()
    current_solution = warm_solution.copy_genes()
    current_record = evaluator.evaluate(
        current_solution, caller="MAIN_CANDIDATE"
    )
    if not current_record.result.feasible or current_record.result.decoded_state is None:
        raise RuntimeError(
            "warm-start LEVEL 1评价失败: "
            + current_record.result.infeasibility_reason
        )
    best_solution = warm_solution.copy_genes()
    best_result = current_record.result
    # 正式基准采用Phase A已确认值；LEVEL 1 decoded中的毫秒级偏差只用于搜索。
    warm_f = FORMAL_WARM_F
    temperature = -(
        config.initial_worse_fraction * warm_f
    ) / math.log(config.initial_accept_probability)

    destroy_names = (
        "random_removal",
        "related_removal",
        "worst_removal",
        "whole_sortie_removal",
        "route_segment_removal",
        "communication_hotspot_removal",
    )
    repair_names = (
        "greedy_insertion",
        "regret2_insertion",
        "regret3_insertion",
        "deadline_first_insertion",
        "communication_aware_insertion",
    )
    destroy_stats = {
        name: AdaptiveOperatorStat("destroy", name) for name in destroy_names
    }
    repair_stats = {
        name: AdaptiveOperatorStat("repair", name) for name in repair_names
    }
    destroy_functions = {
        "random_removal": p2_improved.random_removal,
        "related_removal": p2_improved.related_removal,
        "worst_removal": p2_improved.worst_removal,
        "whole_sortie_removal": p2_improved.route_removal,
        "route_segment_removal": route_segment_removal,
    }
    repair_functions = {
        "greedy_insertion": p2_improved.greedy_insertion,
        "regret2_insertion": p2_improved.regret2_insertion,
        "regret3_insertion": p2_improved.regret3_insertion,
        "deadline_first_insertion": p2_improved.deadline_first_insertion,
    }

    history: List[dict] = []
    feasible_candidates = 0
    infeasible_candidates = 0
    accepted_improving = 0
    accepted_worsening = 0
    accepted_equal = 0
    rejected = 0
    provisional_best_rows: List[dict] = []
    provisional_best_transport_tables: List[pd.DataFrame] = []
    provisional_best_relay_tables: List[pd.DataFrame] = []
    for iteration in range(1, config.iterations + 1):
        iteration_started = perf_counter()
        calls_before = evaluator.request_count
        hits_before = evaluator.hit_count
        misses_before = evaluator.miss_count
        segment_before = lazy_cache.snapshot()
        level1_time_before = sum(
            row.total_s for row in evaluator.caller_profiles.values()
        )
        destroy_name = _roulette_choice(destroy_stats, rng)
        repair_name = _roulette_choice(repair_stats, rng)
        destroy_stat = destroy_stats[destroy_name]
        repair_stat = repair_stats[repair_name]
        destroy_stat.calls += 1
        destroy_stat.segment_calls += 1
        repair_stat.calls += 1
        repair_stat.segment_calls += 1

        current_decoded = current_record.result.decoded_state
        if current_decoded is None:
            raise AssertionError("当前可行解缺少decoded state")
        if destroy_name == "communication_hotspot_removal":
            partial, removed = communication_hotspot_removal(
                current_solution,
                current_record.result,
                context,
                geometry_cache,
                config,
                rng,
            )
        else:
            partial, removed = destroy_functions[destroy_name](
                current_solution.transport_sorties,
                current_decoded.transport_evaluation,
                context.transport_data,
                rng,
            )

        candidate_solution: Optional[Problem3Solution] = None
        candidate_record: Optional[CachedEvaluation] = None
        if repair_name == "communication_aware_insertion":
            candidate_record = communication_aware_repair(
                partial,
                removed,
                context,
                elite_pool,
                cheap_proxy,
                evaluator,
                config,
                search_performance,
            )
            if candidate_record is not None and candidate_record.result.decoded_state:
                candidate_solution = Problem3Solution(
                    tuple(candidate_record.result.decoded_state.transport_evaluation.solution),
                    candidate_record.result.decoded_state,
                )
        else:
            repaired = repair_functions[repair_name](
                partial, removed, context.transport_data, rng
            )
            if repaired is not None:
                candidate_solution = Problem3Solution(tuple(repaired))
                caller = {
                    "greedy_insertion": "GREEDY_REPAIR",
                    "regret2_insertion": "REGRET2_REPAIR",
                    "regret3_insertion": "REGRET3_REPAIR",
                    "deadline_first_insertion": "DEADLINE_REPAIR",
                }[repair_name]
                candidate_record = evaluator.evaluate(
                    candidate_solution, caller=caller
                )
                search_performance.repaired_box_counts.append(len(removed))
                search_performance.repair_full_calls.append(1)

        sigma_name = "none"
        local_name = "none"
        if (
            candidate_solution is not None
            and candidate_record is not None
            and candidate_record.result.feasible
        ):
            feasible_candidates += 1
            destroy_stat.feasible += 1
            repair_stat.feasible += 1
            if rng.random() < config.sigma_local_probability:
                candidate_solution, candidate_record, sigma_name = sigma_local_move(
                    candidate_solution, candidate_record, evaluator, rng
                )
            if rng.random() < config.local_search_probability:
                candidate_solution, candidate_record, local_name = limited_local_search(
                    candidate_solution,
                    candidate_record,
                    context,
                    elite_pool,
                    cheap_proxy,
                    evaluator,
                    config,
                    rng,
                    search_performance,
                )
        else:
            infeasible_candidates += 1
            # Repair 无可行插入或 fast decoder 拒绝都计入不可行分布；前者
            # 没有更细的模型失败信息，按约定归入 other。
            if candidate_record is None:
                evaluator.infeasibility_counts["other"] += 1

        accepted = False
        new_best = False
        candidate_f = (
            candidate_record.result.objective
            if candidate_record is not None and candidate_record.result.feasible
            else math.inf
        )
        old_current_f = current_record.result.objective
        if math.isfinite(candidate_f):
            delta = candidate_f - old_current_f
            accepted = delta <= 0.0 or rng.random() < math.exp(-delta / temperature)
            if accepted and candidate_solution is not None and candidate_record is not None:
                current_solution, current_record = candidate_solution, candidate_record
                destroy_stat.accepted += 1
                repair_stat.accepted += 1
                if delta < -OBJECTIVE_COMPARISON_TOLERANCE:
                    accepted_improving += 1
                    destroy_stat.improving += 1
                    repair_stat.improving += 1
                elif delta > OBJECTIVE_COMPARISON_TOLERANCE:
                    accepted_worsening += 1
                else:
                    accepted_equal += 1
                if (
                    candidate_f
                    < best_result.objective - OBJECTIVE_COMPARISON_TOLERANCE
                ):
                    best_solution = candidate_solution.copy_genes()
                    best_result = candidate_record.result
                    new_best = True
                    destroy_stat.new_best += 1
                    repair_stat.new_best += 1
        if not accepted:
            rejected += 1

        if new_best:
            score = config.sigma_best
        elif (
            accepted
            and candidate_f
            < old_current_f - OBJECTIVE_COMPARISON_TOLERANCE
        ):
            score = config.sigma_improve
        elif accepted:
            score = config.sigma_accept
        else:
            score = config.sigma_reject
        destroy_stat.segment_score += score
        repair_stat.segment_score += score
        if (
            accepted
            and candidate_f
            < old_current_f - OBJECTIVE_COMPARISON_TOLERANCE
        ):
            destroy_stat.successes += 1
            repair_stat.successes += 1
        if iteration % config.segment_length == 0:
            _update_adaptive_weights(destroy_stats, config)
            _update_adaptive_weights(repair_stats, config)

        if new_best:
            provisional = best_result.decoded_state
            if provisional is None:
                raise AssertionError("provisional best缺少decoded state")
            provisional_best_rows.append(
                {
                    "Iteration": iteration,
                    "F_LEVEL1": provisional.objective,
                    "Z1": provisional.objectives[0],
                    "Z2_s": provisional.objectives[1],
                    "Z3_kWh": provisional.objectives[2],
                    "Z4": provisional.objectives[3],
                    "Transport_Sorties": provisional.transport_evaluation.z4_sorties,
                    "Relay_Sorties": len(provisional.relay_sorties),
                    "Relay_Positions": ";".join(
                        provisional.selected_candidate_ids
                    ),
                }
            )
            transport_snapshot = _transport_sorties_table(
                best_solution, provisional
            )
            transport_snapshot.insert(0, "Provisional_Best_Iteration", iteration)
            provisional_best_transport_tables.append(transport_snapshot)
            relay_snapshot = baseline._sorties_table(provisional.relay_sorties)
            relay_snapshot.insert(0, "Provisional_Best_Iteration", iteration)
            provisional_best_relay_tables.append(relay_snapshot)

        current_decoded = current_record.result.decoded_state
        if current_decoded is None:
            raise AssertionError("接受后的当前解缺少decoded state")
        iteration_elapsed = perf_counter() - iteration_started
        iteration_requests = evaluator.request_count - calls_before
        iteration_hits = evaluator.hit_count - hits_before
        iteration_misses = evaluator.miss_count - misses_before
        segment_after = lazy_cache.snapshot()
        level1_time = sum(
            row.total_s for row in evaluator.caller_profiles.values()
        ) - level1_time_before
        candidate_decoded = (
            candidate_record.result.decoded_state
            if candidate_record is not None
            else None
        )
        if accepted:
            accepted_delta = candidate_f - old_current_f
            if accepted_delta < -OBJECTIVE_COMPARISON_TOLERANCE:
                acceptance_type = "improved"
            elif accepted_delta > OBJECTIVE_COMPARISON_TOLERANCE:
                acceptance_type = "worse-SA"
            else:
                acceptance_type = "equal"
        else:
            acceptance_type = "rejected"
        infeasibility_reason = ""
        if not math.isfinite(candidate_f):
            infeasibility_reason = (
                candidate_record.result.infeasibility_reason
                if candidate_record is not None
                else "repair returned no complete solution"
            )
        history.append(
            {
                "Iteration": iteration,
                "Current_F": current_record.result.objective,
                "Best_F": best_result.objective,
                "Z1": current_decoded.objectives[0],
                "Z2": current_decoded.objectives[1],
                "Z3": current_decoded.objectives[2],
                "Z4": current_decoded.objectives[3],
                "Destroy_Operator": destroy_name,
                "Repair_Operator": repair_name,
                "Sigma_Operator": sigma_name,
                "Local_Operator": local_name,
                "Accepted": accepted,
                "Feasible": math.isfinite(candidate_f),
                "New_Global_Best": new_best,
                "Candidate_F": candidate_f,
                "Candidate_Z1": (
                    candidate_decoded.objectives[0]
                    if candidate_decoded is not None else math.nan
                ),
                "Candidate_Z2": (
                    candidate_decoded.objectives[1]
                    if candidate_decoded is not None else math.nan
                ),
                "Candidate_Z3": (
                    candidate_decoded.objectives[2]
                    if candidate_decoded is not None else math.nan
                ),
                "Candidate_Z4": (
                    candidate_decoded.objectives[3]
                    if candidate_decoded is not None else math.nan
                ),
                "Acceptance_Type": acceptance_type,
                "Infeasibility_Reason": infeasibility_reason,
                "Temperature": temperature,
                "Evaluation_Time_s": iteration_elapsed,
                "LEVEL1_Calls": iteration_requests,
                "LEVEL1_Time_s": level1_time,
                "Cache_Hits": iteration_hits,
                "Cache_Misses": iteration_misses,
                "Cache_Hit": iteration_hits > 0,
                "New_Segments_Computed": (
                    segment_after[0] - segment_before[0]
                ),
                "Segment_Cache_Hits": segment_after[1] - segment_before[1],
                "Segment_Cache_Misses": segment_after[2] - segment_before[2],
                "Iteration_Time_s": iteration_elapsed,
            }
        )
        line = (
            f"iter={iteration:03d}, current_F={current_record.result.objective:.10f}, "
            f"best_F={best_result.objective:.10f}, "
            f"Z=({current_decoded.objectives[0]:.6f},"
            f"{current_decoded.objectives[1]:.6f},"
            f"{current_decoded.objectives[2]:.6f},"
            f"{current_decoded.objectives[3]:.0f}), "
            f"destroy={destroy_name}, repair={repair_name}, "
            f"acceptance={acceptance_type}, feasible={math.isfinite(candidate_f)}, "
            f"new_segments={segment_after[0] - segment_before[0]}, "
            f"level1_calls={iteration_requests}, level1_s={level1_time:.3f}, "
            f"iteration_s={iteration_elapsed:.3f}"
        )
        _phase_b_log(line)
        if iteration % config.log_interval == 0:
            print(line, flush=True)
        temperature *= config.cooling_rate

    print("Phase B final-best standard audit...", flush=True)
    final_started = perf_counter()
    provisional_exact_result = evaluate_problem3_solution(
        best_solution,
        context,
        geometry_cache,
        elite_pool,
        weights,
        verify_exact=True,
    )
    final_audit_s = perf_counter() - final_started
    provisional_exact = provisional_exact_result.decoded_state
    provisional_standard_feasible = (
        provisional_exact_result.feasible
        and provisional_exact is not None
        and provisional_exact.communication_check.uncovered_duration_s
        <= core.TIME_TOLERANCE_S
    )
    if provisional_exact is not None and (
        provisional_exact.communication_check.uncovered_duration_s
        > core.TIME_TOLERANCE_S
    ):
        timeline = provisional_exact.communication_check.timeline
        failed = timeline.loc[timeline["Combined_Available"].eq(0)]
        _phase_b_log(
            "PROVISIONAL LEVEL2 COMMUNICATION FAILED: "
            + failed.head(20).to_json(orient="records", force_ascii=False)
        )
    if not provisional_standard_feasible:
        _phase_b_log(
            "PROVISIONAL LEVEL2 FAILED; formal warm start retained: "
            + (
                provisional_exact_result.infeasibility_reason
                or "standard feasibility/continuous communication failed"
            )
        )

    runtime = perf_counter() - started
    strictly_better = bool(
        provisional_standard_feasible
        and provisional_exact is not None
        and provisional_exact.objective
        < warm_f - OBJECTIVE_COMPARISON_TOLERANCE
    )
    if strictly_better:
        exact = provisional_exact
        final_solution = best_solution
        final_source = "LEVEL2_PROVISIONAL_BEST"
    else:
        exact = warm_decoded
        final_solution = warm_solution
        final_source = "FORMAL_WARM_START"
    if final_source == "FORMAL_WARM_START":
        final_report_objectives = FORMAL_WARM_OBJECTIVES
        final_report_normalized = FORMAL_WARM_NORMALIZED
        final_report_f = FORMAL_WARM_F
    else:
        final_report_objectives = exact.objectives
        final_report_normalized = exact.normalized
        final_report_f = exact.objective
    relay_position_ids = ";".join(exact.selected_candidate_ids)
    history_table = pd.DataFrame(history)
    iteration_times = history_table["Iteration_Time_s"].to_numpy(dtype=float)
    final_segment_counters = lazy_cache.snapshot()
    final_cached_segments = len(lazy_cache.cache.segment_profiles)
    best_level1_iteration = (
        int(provisional_best_rows[-1]["Iteration"])
        if provisional_best_rows else 0
    )
    summary = pd.DataFrame(
        [
            {"Metric": "Iterations", "Value": config.iterations},
            {"Metric": "Random_Seed", "Value": config.random_seed},
            {"Metric": "Objective_Comparison_Tolerance", "Value": OBJECTIVE_COMPARISON_TOLERANCE},
            {"Metric": "Final_Source", "Value": final_source},
            {"Metric": "Warm_Start_F", "Value": warm_f},
            {"Metric": "Warm_Start_Z1", "Value": FORMAL_WARM_OBJECTIVES[0]},
            {"Metric": "Warm_Start_Z2_s", "Value": FORMAL_WARM_OBJECTIVES[1]},
            {"Metric": "Warm_Start_Z3_kWh", "Value": FORMAL_WARM_OBJECTIVES[2]},
            {"Metric": "Warm_Start_Z4", "Value": FORMAL_WARM_OBJECTIVES[3]},
            {"Metric": "Best_LEVEL1_Iteration", "Value": best_level1_iteration},
            {"Metric": "Best_LEVEL1_F", "Value": best_result.objective},
            {"Metric": "Best_LEVEL1_Z1", "Value": best_result.decoded_state.objectives[0]},
            {"Metric": "Best_LEVEL1_Z2_s", "Value": best_result.decoded_state.objectives[1]},
            {"Metric": "Best_LEVEL1_Z3_kWh", "Value": best_result.decoded_state.objectives[2]},
            {"Metric": "Best_LEVEL1_Z4", "Value": best_result.decoded_state.objectives[3]},
            {"Metric": "Final_Best_F", "Value": final_report_f},
            {"Metric": "Strictly_Better_Than_Warm", "Value": strictly_better},
            {"Metric": "Delta_F_vs_Warm", "Value": final_report_f - warm_f},
            {"Metric": "Delta_Z1_vs_Warm", "Value": final_report_objectives[0] - FORMAL_WARM_OBJECTIVES[0]},
            {"Metric": "Delta_Z2_vs_Warm_s", "Value": final_report_objectives[1] - FORMAL_WARM_OBJECTIVES[1]},
            {"Metric": "Delta_Z3_vs_Warm_kWh", "Value": final_report_objectives[2] - FORMAL_WARM_OBJECTIVES[2]},
            {"Metric": "Delta_Z4_vs_Warm", "Value": final_report_objectives[3] - FORMAL_WARM_OBJECTIVES[3]},
            {"Metric": "Best_Z1", "Value": final_report_objectives[0]},
            {"Metric": "Best_Z2_s", "Value": final_report_objectives[1]},
            {"Metric": "Best_Z3_kWh", "Value": final_report_objectives[2]},
            {"Metric": "Best_Z4", "Value": final_report_objectives[3]},
            {"Metric": "Best_Transport_Sorties", "Value": exact.transport_evaluation.z4_sorties},
            {"Metric": "Best_Relay_Sorties", "Value": len(exact.relay_sorties)},
            {"Metric": "Best_Relay_Positions", "Value": relay_position_ids},
            {"Metric": "Feasible_Iteration_Candidates", "Value": feasible_candidates},
            {"Metric": "Infeasible_Iteration_Candidates", "Value": infeasible_candidates},
            {"Metric": "Accepted_Improving_Moves", "Value": accepted_improving},
            {"Metric": "Accepted_Worsening_Moves", "Value": accepted_worsening},
            {"Metric": "Accepted_Equal_Moves", "Value": accepted_equal},
            {"Metric": "Rejected_Moves", "Value": rejected},
            {"Metric": "Mean_Iteration_Time_s", "Value": float(iteration_times.mean())},
            {"Metric": "Median_Iteration_Time_s", "Value": float(np.median(iteration_times))},
            {"Metric": "Maximum_Iteration_Time_s", "Value": float(iteration_times.max())},
            {"Metric": "Initial_Cached_Segments", "Value": initial_cached_segments},
            {"Metric": "Final_Cached_Segments", "Value": final_cached_segments},
            {"Metric": "New_Segments_Computed", "Value": final_segment_counters[0] - segment_counter_start[0]},
            {"Metric": "Segment_Cache_Hits", "Value": final_segment_counters[1] - segment_counter_start[1]},
            {"Metric": "Segment_Cache_Misses", "Value": final_segment_counters[2] - segment_counter_start[2]},
            {"Metric": "Solution_Cache_Hit_Rate", "Value": evaluator.hit_count / max(evaluator.request_count, 1)},
            {"Metric": "TopK_Full_Evaluator_Calls", "Value": evaluator.top_k_full_calls},
            {"Metric": "Warm_Exact_Audit_s", "Value": warm_audit_s},
            {"Metric": "Final_Exact_Audit_s", "Value": final_audit_s},
            {"Metric": "Uncovered_Communication_Duration_s", "Value": exact.communication_check.uncovered_duration_s},
            {"Metric": "Total_Runtime_s", "Value": runtime},
        ]
    )
    relay_table = baseline._sorties_table(exact.relay_sorties)
    candidate_by_position = {
        row.position_id: row.candidate_id for row in exact.relay_positions
    }
    if not relay_table.empty:
        relay_table.insert(
            relay_table.columns.get_loc("Position_ID") + 1,
            "Candidate_ID",
            relay_table["Position_ID"].map(candidate_by_position),
        )
    objective_table = pd.DataFrame(
        [
            {
                "Z1": final_report_objectives[0],
                "Z2_s": final_report_objectives[1],
                "Z3_kWh": final_report_objectives[2],
                "Z4": final_report_objectives[3],
                "S1": PROBLEM3_NORMALIZATION_SCALES[0],
                "S2": PROBLEM3_NORMALIZATION_SCALES[1],
                "S3": PROBLEM3_NORMALIZATION_SCALES[2],
                "S4": PROBLEM3_NORMALIZATION_SCALES[3],
                "Normalized_Z1": final_report_normalized[0],
                "Normalized_Z2": final_report_normalized[1],
                "Normalized_Z3": final_report_normalized[2],
                "Normalized_Z4": final_report_normalized[3],
                "Lambda1": weights[0],
                "Lambda2": weights[1],
                "Lambda3": weights[2],
                "Lambda4": weights[3],
                "F": final_report_f,
            }
        ]
    )
    infeasibility_rows = [
        {"Reason": name, "Count": evaluator.infeasibility_counts.get(name, 0)}
        for name in (
            "transport_capacity",
            "transport_energy",
            "hard_deadline",
            "transport_resource",
            "relay_coverage",
            "relay_energy",
            "relay_uav_resource",
            "relay_component_resource",
            "other",
        )
    ]
    cache_statistics = pd.concat(
        [
            pd.DataFrame(
                [
                    {"Metric": "Signature_OK", "Value": lazy_cache.cache.signature == lazy_cache.expected_signature},
                    {"Metric": "Initial_Cached_Segments", "Value": initial_cached_segments},
                    {"Metric": "Final_Cached_Segments", "Value": final_cached_segments},
                    {"Metric": "Segment_Universe", "Value": lazy_cache.cache.actual_directed_segment_count},
                    {"Metric": "New_Segments_Computed", "Value": final_segment_counters[0] - segment_counter_start[0]},
                    {"Metric": "Segment_Cache_Hits", "Value": final_segment_counters[1] - segment_counter_start[1]},
                    {"Metric": "Segment_Cache_Misses", "Value": final_segment_counters[2] - segment_counter_start[2]},
                    {"Metric": "Cache_File_Size_Bytes", "Value": segment_cache.CACHE_FILE.stat().st_size},
                ]
            ),
            _evaluation_statistics_table(evaluator),
            geometry_cache.manifest(),
        ],
        ignore_index=True,
    )
    provisional_summary = pd.DataFrame(provisional_best_rows)
    provisional_transport = (
        pd.concat(provisional_best_transport_tables, ignore_index=True)
        if provisional_best_transport_tables else pd.DataFrame()
    )
    provisional_relay = (
        pd.concat(provisional_best_relay_tables, ignore_index=True)
        if provisional_best_relay_tables else pd.DataFrame()
    )
    transport_table = _transport_sorties_table(final_solution, exact)
    paths.ensure_output_path(PHASE_B_OUTPUT_FILE)
    PHASE_B_OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    paths.ensure_output_path(PHASE_B_OUTPUT_FILE)
    with pd.ExcelWriter(PHASE_B_OUTPUT_FILE, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        transport_table.to_excel(writer, sheet_name="Transport_Sorties", index=False)
        transport_table.to_excel(writer, sheet_name="Transport_Schedule", index=False)
        baseline._positions_table(exact.relay_positions).to_excel(
            writer, sheet_name="Relay_Positions", index=False
        )
        relay_table.to_excel(writer, sheet_name="Relay_Sorties", index=False)
        baseline._tasks_table(exact.relay_service_tasks).to_excel(
            writer, sheet_name="Relay_Service", index=False
        )
        baseline._energy_table(
            exact.relay_sorties, context.relay_parameters
        ).to_excel(writer, sheet_name="Relay_Energy_Components", index=False)
        baseline._resource_schedule_table(
            exact.relay_sorties, context.relay_parameters
        ).to_excel(writer, sheet_name="Relay_Resource_Schedule", index=False)
        exact.communication_check.timeline.to_excel(
            writer, sheet_name="Communication_Check", index=False
        )
        objective_table.to_excel(writer, sheet_name="Objective", index=False)
        history_table.to_excel(writer, sheet_name="ALNS_History", index=False)
        _operator_statistics_table(destroy_stats, repair_stats).to_excel(
            writer, sheet_name="Operator_Statistics", index=False
        )
        pd.DataFrame(infeasibility_rows).to_excel(
            writer, sheet_name="Infeasibility_Statistics", index=False
        )
        cache_statistics.to_excel(writer, sheet_name="Cache_Statistics", index=False)
        layer_profiler.table().to_excel(
            writer, sheet_name="Layer_Timing", index=False
        )
        evaluator.caller_table().to_excel(
            writer, sheet_name="Evaluator_Callers", index=False
        )
        search_performance.table().to_excel(
            writer, sheet_name="Search_Performance", index=False
        )
        exact.feasibility.to_excel(
            writer, sheet_name="Final_Feasibility_Check", index=False
        )
        provisional_summary.to_excel(
            writer, sheet_name="Provisional_Best", index=False
        )
        provisional_transport.to_excel(
            writer, sheet_name="Provisional_Transport", index=False
        )
        provisional_relay.to_excel(
            writer, sheet_name="Provisional_Relay", index=False
        )
    final_line = (
        f"DONE warm_F={warm_f:.10f}, best_F={final_report_f:.10f}, "
        f"final_source={final_source}, strictly_better={strictly_better}, "
        f"Z={final_report_objectives}, "
        f"transport_sorties={exact.transport_evaluation.z4_sorties}, "
        f"relay_sorties={len(exact.relay_sorties)}, positions={relay_position_ids}, "
        f"feasible={feasible_candidates}, infeasible={infeasible_candidates}, "
        f"cache={initial_cached_segments}->{final_cached_segments}, "
        f"segment_hits={final_segment_counters[1] - segment_counter_start[1]}, "
        f"segment_misses={final_segment_counters[2] - segment_counter_start[2]}, "
        f"runtime_s={runtime:.3f}"
    )
    _phase_b_log(final_line)
    print(final_line, flush=True)
    print(f"Workbook={PHASE_B_OUTPUT_FILE}", flush=True)
    print(f"Log={PHASE_B_LOG_FILE}", flush=True)
    return exact


def run_phase_b_profile(iterations: int = 2) -> None:
    """最多2轮的 LEVEL 0/1 性能验证；明确不运行 LEVEL 2 标准复核。"""
    if not 1 <= iterations <= 2:
        raise ValueError("性能验证只允许1或2次迭代")
    output = paths.output_file("problem3", "problem3_alns_profile.xlsx")
    config = replace(
        PhaseBConfig(),
        iterations=iterations,
        communication_top_k=2,
        local_top_k=2,
        max_local_trials=3,
        local_search_probability=0.20,
    )
    started = perf_counter()
    rng = random.Random(config.random_seed)
    context = load_phase_a_context()
    geometry_cache = GeometryCommunicationCache(context)
    warm_solution = load_baseline_warm_start()
    warm_transport, warm_trajectories = decode_transport(warm_solution, context)
    warm_demands, _ = relay.load_step3_demands()
    elite_pool = load_saved_elite_pool(
        context, geometry_cache, warm_demands, ELITE_POOL_FILE
    )
    attach_lazy_persistent_segment_cache(context, geometry_cache, elite_pool)
    cheap_proxy = build_cheap_communication_proxy(context, elite_pool)
    layer_profiler = EvaluationLayerProfiler()
    evaluator = SolutionEvaluationCache(
        context,
        geometry_cache,
        elite_pool,
        config.objective_weights,
        layer_profiler,
    )
    performance = SearchPerformanceCounters()
    rows: List[dict] = []

    # 基准 LEVEL 1：不调用 standard final audit。
    current_solution = warm_solution.copy_genes()
    current = evaluator.evaluate(current_solution, caller="MAIN_CANDIDATE")
    if not current.result.feasible:
        raise RuntimeError("性能验证的warm LEVEL 1评价失败")

    for iteration in range(1, iterations + 1):
        before_calls = evaluator.request_count
        iteration_started = perf_counter()
        decoded = current.result.decoded_state
        assert decoded is not None
        if iteration == 1:
            partial, removed = p2_improved.random_removal(
                current_solution.transport_sorties,
                decoded.transport_evaluation,
                context.transport_data,
                rng,
            )
            repaired = p2_improved.greedy_insertion(
                partial, removed, context.transport_data, rng
            )
            performance.repaired_box_counts.append(len(removed))
            performance.repair_full_calls.append(1 if repaired is not None else 0)
            if repaired is not None:
                candidate_solution = Problem3Solution(tuple(repaired))
                candidate = evaluator.evaluate(
                    candidate_solution, caller="GREEDY_REPAIR"
                )
            else:
                candidate_solution, candidate = current_solution, current
            if candidate.result.feasible:
                candidate_solution, candidate, _ = limited_local_search(
                    candidate_solution,
                    candidate,
                    context,
                    elite_pool,
                    cheap_proxy,
                    evaluator,
                    config,
                    rng,
                    performance,
                )
        else:
            partial, removed = route_segment_removal(
                current_solution.transport_sorties,
                decoded.transport_evaluation,
                context.transport_data,
                rng,
            )
            candidate = communication_aware_repair(
                partial,
                removed,
                context,
                elite_pool,
                cheap_proxy,
                evaluator,
                config,
                performance,
            )
            if candidate is not None and candidate.result.decoded_state is not None:
                candidate_solution = Problem3Solution(
                    tuple(candidate.result.decoded_state.transport_evaluation.solution),
                    candidate.result.decoded_state,
                )
                candidate_solution, candidate, _ = sigma_local_move(
                    candidate_solution, candidate, evaluator, rng
                )
            else:
                candidate_solution, candidate = current_solution, current
        if candidate.result.feasible:
            current_solution, current = candidate_solution, candidate
        rows.append(
            {
                "Iteration": iteration,
                "Repaired_Boxes": len(removed),
                "Level1_Calls": evaluator.request_count - before_calls,
                "Elapsed_s": perf_counter() - iteration_started,
                "Feasible": candidate.result.feasible,
                "F": candidate.result.objective,
            }
        )

    summary = pd.DataFrame(
        [
            {"Metric": "Profile_Iterations", "Value": iterations},
            {"Metric": "LEVEL2_Standard_Audit_Calls", "Value": 0},
            {"Metric": "LEVEL1_Requests", "Value": evaluator.request_count},
            {"Metric": "LEVEL1_Cache_Hits", "Value": evaluator.hit_count},
            {"Metric": "LEVEL1_Cache_Misses", "Value": evaluator.miss_count},
            {"Metric": "Total_Runtime_s", "Value": perf_counter() - started},
        ]
    )
    paths.ensure_output_path(output)
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        pd.DataFrame(rows).to_excel(writer, sheet_name="Iterations", index=False)
        layer_profiler.table().to_excel(writer, sheet_name="Layer_Timing", index=False)
        evaluator.caller_table().to_excel(
            writer, sheet_name="Evaluator_Callers", index=False
        )
        performance.table().to_excel(
            writer, sheet_name="Search_Performance", index=False
        )
    print(f"Profile workbook={output}", flush=True)


def _lazy_test_log(message: str, reset: bool = False) -> None:
    paths.ensure_output_path(LAZY_CACHE_TEST_LOG_FILE)
    LAZY_CACHE_TEST_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LAZY_CACHE_TEST_LOG_FILE.open(
        "w" if reset else "a", encoding="utf-8"
    ) as stream:
        stream.write(message + "\n")


def _warm_start_directed_segments(
    trajectories: Sequence[core.TransportTrajectory],
) -> Tuple[Tuple[str, str], ...]:
    return tuple(
        sorted(
            {
                (phase.start_node, phase.end_node)
                for trajectory in trajectories
                for phase in trajectory.communication_phases
                if phase.phase != core.HANDOVER
            }
        )
    )


def run_lazy_cache_performance_test(iterations: int = 10) -> Problem3DecodedState:
    """预热warm-start实际航段并运行10轮原Phase B ALNS；绝不调用LEVEL 2。"""
    if iterations != 10:
        raise ValueError("lazy persistent cache性能测试固定只运行10轮")
    config = replace(PhaseBConfig(), iterations=iterations)
    weights = validate_objective_weights(config.objective_weights)
    rng = random.Random(config.random_seed)
    started = perf_counter()
    _lazy_test_log(
        f"lazy cache 10-iteration test start; seed={config.random_seed}; "
        f"weights={weights}",
        reset=True,
    )
    p2_improved.CONFIG = replace(
        p2_improved.CONFIG,
        random_seed=config.random_seed,
        destroy_fraction_min=config.destroy_fraction_min,
        destroy_fraction_max=config.destroy_fraction_max,
        objective_weights=weights,
    )

    context = load_phase_a_context()
    geometry_cache = GeometryCommunicationCache(context)
    warm_solution = load_baseline_warm_start()
    warm_transport, warm_trajectories = decode_transport(warm_solution, context)
    if not warm_transport.feasible:
        raise RuntimeError("warm-start运输解码失败")
    step3_demands, _ = relay.load_step3_demands()
    elite_pool = load_saved_elite_pool(
        context, geometry_cache, step3_demands, ELITE_POOL_FILE
    )
    lazy_cache = attach_lazy_persistent_segment_cache(
        context, geometry_cache, elite_pool
    )
    warm_segments = _warm_start_directed_segments(warm_trajectories)
    prewarm_before = lazy_cache.snapshot()
    newly_prewarmed, prewarm_s = lazy_cache.prewarm(warm_segments, progress=True)
    prewarm_after = lazy_cache.snapshot()
    cache_size_bytes = segment_cache.CACHE_FILE.stat().st_size
    prewarm_line = (
        f"warm_segments={len(warm_segments)}, newly_computed={newly_prewarmed}, "
        f"hits={prewarm_after[1] - prewarm_before[1]}, "
        f"misses={prewarm_after[2] - prewarm_before[2]}, "
        f"prewarm_s={prewarm_s:.6f}, cache_size_bytes={cache_size_bytes}"
    )
    print(prewarm_line, flush=True)
    _lazy_test_log(prewarm_line)

    cheap_proxy = build_cheap_communication_proxy(context, elite_pool)
    layer_profiler = EvaluationLayerProfiler()
    evaluator = SolutionEvaluationCache(
        context, geometry_cache, elite_pool, weights, layer_profiler
    )
    search_performance = SearchPerformanceCounters()
    current_solution = warm_solution.copy_genes()
    current_record = evaluator.evaluate(
        current_solution, caller="MAIN_CANDIDATE"
    )
    if not current_record.result.feasible or current_record.result.decoded_state is None:
        raise RuntimeError(
            "warm-start LEVEL 1评价失败: "
            + current_record.result.infeasibility_reason
        )
    warm_decoded = current_record.result.decoded_state
    expected = (0.0, 6985.307588484, 68.808128466, 23.0)
    if any(
        abs(value - reference) > (0.05 if index in (1, 2) else 1e-6)
        for index, (value, reference) in enumerate(
            zip(warm_decoded.objectives, expected)
        )
    ):
        raise RuntimeError(
            f"warm-start LEVEL 1目标口径不一致: {warm_decoded.objectives}"
        )
    best_solution = warm_solution.copy_genes()
    best_result = current_record.result
    warm_f = current_record.result.objective
    temperature = -(
        config.initial_worse_fraction * warm_f
    ) / math.log(config.initial_accept_probability)

    destroy_names = (
        "random_removal",
        "related_removal",
        "worst_removal",
        "whole_sortie_removal",
        "route_segment_removal",
        "communication_hotspot_removal",
    )
    repair_names = (
        "greedy_insertion",
        "regret2_insertion",
        "regret3_insertion",
        "deadline_first_insertion",
        "communication_aware_insertion",
    )
    destroy_stats = {
        name: AdaptiveOperatorStat("destroy", name) for name in destroy_names
    }
    repair_stats = {
        name: AdaptiveOperatorStat("repair", name) for name in repair_names
    }
    destroy_functions = {
        "random_removal": p2_improved.random_removal,
        "related_removal": p2_improved.related_removal,
        "worst_removal": p2_improved.worst_removal,
        "whole_sortie_removal": p2_improved.route_removal,
        "route_segment_removal": route_segment_removal,
    }
    repair_functions = {
        "greedy_insertion": p2_improved.greedy_insertion,
        "regret2_insertion": p2_improved.regret2_insertion,
        "regret3_insertion": p2_improved.regret3_insertion,
        "deadline_first_insertion": p2_improved.deadline_first_insertion,
    }

    history: List[dict] = []
    for iteration in range(1, iterations + 1):
        iteration_started = perf_counter()
        segment_before = lazy_cache.snapshot()
        t4_before = sum(layer_profiler.samples_s["T4"])
        t5_before = sum(layer_profiler.samples_s["T5"])
        level1_before = sum(
            row.total_s for row in evaluator.caller_profiles.values()
        )
        requests_before = evaluator.request_count
        solution_hits_before = evaluator.hit_count
        solution_misses_before = evaluator.miss_count

        destroy_name = _roulette_choice(destroy_stats, rng)
        repair_name = _roulette_choice(repair_stats, rng)
        destroy_stat = destroy_stats[destroy_name]
        repair_stat = repair_stats[repair_name]
        destroy_stat.calls += 1
        destroy_stat.segment_calls += 1
        repair_stat.calls += 1
        repair_stat.segment_calls += 1
        current_decoded = current_record.result.decoded_state
        if current_decoded is None:
            raise AssertionError("当前可行解缺少decoded state")

        if destroy_name == "communication_hotspot_removal":
            partial, removed = communication_hotspot_removal(
                current_solution,
                current_record.result,
                context,
                geometry_cache,
                config,
                rng,
            )
        else:
            partial, removed = destroy_functions[destroy_name](
                current_solution.transport_sorties,
                current_decoded.transport_evaluation,
                context.transport_data,
                rng,
            )

        candidate_solution: Optional[Problem3Solution] = None
        candidate_record: Optional[CachedEvaluation] = None
        if repair_name == "communication_aware_insertion":
            candidate_record = communication_aware_repair(
                partial,
                removed,
                context,
                elite_pool,
                cheap_proxy,
                evaluator,
                config,
                search_performance,
            )
            if candidate_record is not None and candidate_record.result.decoded_state:
                candidate_solution = Problem3Solution(
                    tuple(
                        candidate_record.result.decoded_state.transport_evaluation.solution
                    ),
                    candidate_record.result.decoded_state,
                )
        else:
            repaired = repair_functions[repair_name](
                partial, removed, context.transport_data, rng
            )
            if repaired is not None:
                candidate_solution = Problem3Solution(tuple(repaired))
                caller = {
                    "greedy_insertion": "GREEDY_REPAIR",
                    "regret2_insertion": "REGRET2_REPAIR",
                    "regret3_insertion": "REGRET3_REPAIR",
                    "deadline_first_insertion": "DEADLINE_REPAIR",
                }[repair_name]
                candidate_record = evaluator.evaluate(
                    candidate_solution, caller=caller
                )
                search_performance.repaired_box_counts.append(len(removed))
                search_performance.repair_full_calls.append(1)

        sigma_name = "none"
        local_name = "none"
        if (
            candidate_solution is not None
            and candidate_record is not None
            and candidate_record.result.feasible
        ):
            destroy_stat.feasible += 1
            repair_stat.feasible += 1
            if rng.random() < config.sigma_local_probability:
                candidate_solution, candidate_record, sigma_name = sigma_local_move(
                    candidate_solution, candidate_record, evaluator, rng
                )
            if rng.random() < config.local_search_probability:
                candidate_solution, candidate_record, local_name = limited_local_search(
                    candidate_solution,
                    candidate_record,
                    context,
                    elite_pool,
                    cheap_proxy,
                    evaluator,
                    config,
                    rng,
                    search_performance,
                )

        accepted = False
        new_best = False
        candidate_f = (
            candidate_record.result.objective
            if candidate_record is not None and candidate_record.result.feasible
            else math.inf
        )
        old_current_f = current_record.result.objective
        if math.isfinite(candidate_f):
            delta = candidate_f - old_current_f
            accepted = delta <= 0.0 or rng.random() < math.exp(-delta / temperature)
            if accepted and candidate_solution is not None and candidate_record is not None:
                current_solution, current_record = candidate_solution, candidate_record
                destroy_stat.accepted += 1
                repair_stat.accepted += 1
                if delta < -NUMERIC_TOLERANCE:
                    destroy_stat.improving += 1
                    repair_stat.improving += 1
                if candidate_f < best_result.objective - NUMERIC_TOLERANCE:
                    best_solution = candidate_solution.copy_genes()
                    best_result = candidate_record.result
                    new_best = True
                    destroy_stat.new_best += 1
                    repair_stat.new_best += 1

        score = (
            config.sigma_best
            if new_best
            else config.sigma_improve
            if accepted and candidate_f < old_current_f - NUMERIC_TOLERANCE
            else config.sigma_accept
            if accepted
            else config.sigma_reject
        )
        destroy_stat.segment_score += score
        repair_stat.segment_score += score
        if accepted and candidate_f < old_current_f - NUMERIC_TOLERANCE:
            destroy_stat.successes += 1
            repair_stat.successes += 1
        if iteration % config.segment_length == 0:
            _update_adaptive_weights(destroy_stats, config)
            _update_adaptive_weights(repair_stats, config)

        temperature *= config.cooling_rate
        segment_after = lazy_cache.snapshot()
        current_decoded = current_record.result.decoded_state
        if current_decoded is None:
            raise AssertionError("接受后的当前解缺少decoded state")
        row = {
            "Iteration": iteration,
            "New_Segments_Computed": segment_after[0] - segment_before[0],
            "Segment_Cache_Hits": segment_after[1] - segment_before[1],
            "Segment_Cache_Misses": segment_after[2] - segment_before[2],
            "T4_s": sum(layer_profiler.samples_s["T4"]) - t4_before,
            "T5_s": sum(layer_profiler.samples_s["T5"]) - t5_before,
            "LEVEL1_Time_s": sum(
                value.total_s for value in evaluator.caller_profiles.values()
            )
            - level1_before,
            "Iteration_Time_s": perf_counter() - iteration_started,
            "LEVEL1_Requests": evaluator.request_count - requests_before,
            "Solution_Cache_Hits": evaluator.hit_count - solution_hits_before,
            "Solution_Cache_Misses": evaluator.miss_count - solution_misses_before,
            "Repaired_Boxes": len(removed),
            "Destroy_Operator": destroy_name,
            "Repair_Operator": repair_name,
            "Sigma_Operator": sigma_name,
            "Local_Operator": local_name,
            "Accepted": accepted,
            "Feasible": math.isfinite(candidate_f),
            "New_Global_Best": new_best,
            "Current_F": current_record.result.objective,
            "Best_F": best_result.objective,
            "Z1": current_decoded.objectives[0],
            "Z2": current_decoded.objectives[1],
            "Z3": current_decoded.objectives[2],
            "Z4": current_decoded.objectives[3],
        }
        history.append(row)
        line = (
            f"iter={iteration:02d}, new_segments={row['New_Segments_Computed']}, "
            f"segment_hits={row['Segment_Cache_Hits']}, "
            f"segment_misses={row['Segment_Cache_Misses']}, "
            f"T4={row['T4_s']:.3f}s, T5={row['T5_s']:.3f}s, "
            f"LEVEL1={row['LEVEL1_Time_s']:.3f}s, "
            f"iteration={row['Iteration_Time_s']:.3f}s, "
            f"best_F={row['Best_F']:.10f}"
        )
        print(line, flush=True)
        _lazy_test_log(line)

    runtime = perf_counter() - started
    best_decoded = best_result.decoded_state
    if best_decoded is None:
        raise AssertionError("10轮测试best缺少decoded state")
    final_cache_size = segment_cache.CACHE_FILE.stat().st_size
    summary = pd.DataFrame(
        [
            {"Metric": "Iterations", "Value": iterations},
            {"Metric": "LEVEL2_Standard_Audit_Calls", "Value": 0},
            {"Metric": "Warm_Start_Directed_Segments", "Value": len(warm_segments)},
            {"Metric": "Warm_Start_New_Segments_Computed", "Value": newly_prewarmed},
            {"Metric": "Warm_Start_Prewarm_s", "Value": prewarm_s},
            {"Metric": "Initial_Cache_Load_s", "Value": lazy_cache.load_time_s},
            {"Metric": "Cached_Segments_After_Test", "Value": len(lazy_cache.cache.segment_profiles)},
            {"Metric": "Segment_Universe", "Value": lazy_cache.cache.actual_directed_segment_count},
            {"Metric": "Cache_File_Size_Bytes", "Value": final_cache_size},
            {"Metric": "Warm_Start_F", "Value": warm_f},
            {"Metric": "Best_F", "Value": best_result.objective},
            {"Metric": "Best_Z1", "Value": best_decoded.objectives[0]},
            {"Metric": "Best_Z2_s", "Value": best_decoded.objectives[1]},
            {"Metric": "Best_Z3_kWh", "Value": best_decoded.objectives[2]},
            {"Metric": "Best_Z4", "Value": best_decoded.objectives[3]},
            {"Metric": "Total_Runtime_s", "Value": runtime},
        ]
    )
    paths.ensure_output_path(LAZY_CACHE_TEST_OUTPUT_FILE)
    LAZY_CACHE_TEST_OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    paths.ensure_output_path(LAZY_CACHE_TEST_OUTPUT_FILE)
    with pd.ExcelWriter(LAZY_CACHE_TEST_OUTPUT_FILE, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        pd.DataFrame(history).to_excel(writer, sheet_name="Iterations", index=False)
        layer_profiler.table().to_excel(writer, sheet_name="Layer_Timing", index=False)
        evaluator.caller_table().to_excel(
            writer, sheet_name="Evaluator_Callers", index=False
        )
        search_performance.table().to_excel(
            writer, sheet_name="Search_Performance", index=False
        )
        geometry_cache.manifest().to_excel(
            writer, sheet_name="Cache_Manifest", index=False
        )
        _operator_statistics_table(destroy_stats, repair_stats).to_excel(
            writer, sheet_name="Operator_Statistics", index=False
        )
    final_line = (
        f"DONE iterations=10, LEVEL2_calls=0, cached_segments="
        f"{len(lazy_cache.cache.segment_profiles)}/"
        f"{lazy_cache.cache.actual_directed_segment_count}, "
        f"cache_size_bytes={final_cache_size}, runtime_s={runtime:.3f}"
    )
    print(final_line, flush=True)
    print(f"Workbook={LAZY_CACHE_TEST_OUTPUT_FILE}", flush=True)
    print(f"Log={LAZY_CACHE_TEST_LOG_FILE}", flush=True)
    _lazy_test_log(final_line)
    return best_decoded


def main() -> None:
    parser = argparse.ArgumentParser(description="Problem 3 joint ALNS")
    parser.add_argument(
        "--phase",
        choices=("a", "b", "profile", "lazy10"),
        default="b",
        help=(
            "a=Phase A audit, b=100-iteration formal run, profile=最多2轮性能验证, "
            "lazy10=lazy cache预热及10轮测试"
        ),
    )
    args = parser.parse_args()
    if args.phase == "a":
        run_phase_a()
    elif args.phase == "profile":
        run_phase_b_profile(iterations=2)
    elif args.phase == "lazy10":
        run_lazy_cache_performance_test(iterations=10)
    else:
        run_phase_b_debug()


if __name__ == "__main__":
    main()
