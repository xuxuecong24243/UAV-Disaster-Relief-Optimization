"""问题三 Step 3：问题二运输方案的三维轨迹与G01直连通信检查。

本模块固定读取问题二正式结果，不构造中继候选点、不调度中继资源，
也不运行任何ALNS。运输物理量直接复用问题一、问题二已验证的口径。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
import math
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

import problem1_segment_preprocessing as p1_segment  # noqa: E402
import problem2_alns as p2_core  # noqa: E402

import problem3_communication as communication  # noqa: E402


PROBLEM2_RESULT_FILE = paths.input_file("problem2", "problem2_Z1_priority_result.xlsx")
SEGMENT_FILE = paths.input_file("problem1", "segment_preprocessing.xlsx")
NODE_FILE = paths.raw_file("调度中心与服务区.xlsx")
RESULT_DIR = paths.output_dir("problem3")
OUTPUT_FILE = paths.output_file("problem3", "problem3_transport_communication_check.xlsx")

TIME_TOLERANCE_S = 1e-6
DISTANCE_TOLERANCE_M = 1e-5
POSITION_CACHE_DECIMALS = (10, 10, 6)

PREPARE_LOAD = "PREPARE_LOAD"
CLIMB = "CLIMB"
CRUISE = "CRUISE"
DESCENT = "DESCENT"
HANDOVER = "HANDOVER"


@dataclass(frozen=True)
class Node3D:
    node_id: str
    longitude_deg: float
    latitude_deg: float
    ground_elevation_m: float
    operation_altitude_m: float


@dataclass(frozen=True)
class TransportSortieRecord:
    sortie_id: str
    schedule_order: int
    sortie: p2_core.Sortie
    uav_id: str
    battery_id: str
    start_time_s: float
    takeoff_time_s: float
    return_time_s: float
    reported_duration_s: float


@dataclass(frozen=True)
class TrajectoryPhase:
    phase_id: str
    sortie_id: str
    transport_uav_id: str
    phase: str
    leg_index: int
    start_node: str
    end_node: str
    start_time_s: float
    end_time_s: float
    start_point: communication.Point3D
    end_point: communication.Point3D
    horizontal_distance_m: float = 0.0
    forward_azimuth_deg: float = 0.0
    cruise_altitude_m: Optional[float] = None

    @property
    def duration_s(self) -> float:
        return self.end_time_s - self.start_time_s

    @property
    def communication_required(self) -> bool:
        return self.phase in communication.COMMUNICATION_REQUIRED_PHASES

    def position_at(self, time_s: float) -> communication.Point3D:
        """返回阶段内任意绝对时刻的连续三维位置。"""
        if time_s < self.start_time_s - TIME_TOLERANCE_S or (
            time_s > self.end_time_s + TIME_TOLERANCE_S
        ):
            raise ValueError(f"时刻{time_s}不在轨迹阶段{self.phase_id}内")
        if self.duration_s <= TIME_TOLERANCE_S:
            fraction = 1.0
        else:
            fraction = min(
                1.0,
                max(0.0, (time_s - self.start_time_s) / self.duration_s),
            )

        if self.phase == CRUISE:
            travelled_m = self.horizontal_distance_m * fraction
            longitude, latitude, _ = p1_segment.GEOD.fwd(
                self.start_point.longitude_deg,
                self.start_point.latitude_deg,
                self.forward_azimuth_deg,
                travelled_m,
            )
            return communication.Point3D(
                float(longitude),
                float(latitude),
                self.start_point.altitude_m,
            )

        longitude = self.start_point.longitude_deg
        latitude = self.start_point.latitude_deg
        altitude = self.start_point.altitude_m + fraction * (
            self.end_point.altitude_m - self.start_point.altitude_m
        )
        return communication.Point3D(longitude, latitude, altitude)


@dataclass
class TransportTrajectory:
    record: TransportSortieRecord
    phases: Tuple[TrajectoryPhase, ...]
    service_arrival_times_s: Mapping[str, float]
    service_handover_end_times_s: Mapping[str, float]
    box_delivery_times_s: Mapping[str, float]
    maximum_time_error_s: float

    @property
    def communication_phases(self) -> Tuple[TrajectoryPhase, ...]:
        return tuple(phase for phase in self.phases if phase.communication_required)

    @property
    def communication_start_s(self) -> float:
        return self.communication_phases[0].start_time_s

    @property
    def communication_end_s(self) -> float:
        return self.communication_phases[-1].end_time_s

    @property
    def total_duration_s(self) -> float:
        return self.phases[-1].end_time_s - self.phases[0].start_time_s

    def phase_at(self, time_s: float, communication_only: bool = True) -> TrajectoryPhase:
        phases = self.communication_phases if communication_only else self.phases
        if time_s < phases[0].start_time_s - TIME_TOLERANCE_S or (
            time_s > phases[-1].end_time_s + TIME_TOLERANCE_S
        ):
            raise ValueError(
                f"时刻{time_s}不在架次{self.record.sortie_id}轨迹范围内"
            )
        for phase in phases[:-1]:
            if phase.start_time_s - TIME_TOLERANCE_S <= time_s < phase.end_time_s:
                return phase
        return phases[-1]

    def position_at(self, time_s: float) -> communication.Point3D:
        return self.phase_at(time_s).position_at(time_s)


@dataclass(frozen=True)
class DirectLinkSample:
    time_s: float
    transport_uav_id: str
    sortie_id: str
    phase: str
    longitude_deg: float
    latitude_deg: float
    altitude_m: float
    distance_to_g01_3d_m: float
    terrain_obstruction_b: int
    fspl_db: float
    path_loss_db: float
    maximum_path_loss_db: float
    a_ug: int
    is_stage_boundary: bool
    is_refined_transition: bool = False


@dataclass(frozen=True)
class OutageInterval:
    outage_id: str
    transport_uav_id: str
    sortie_id: str
    start_time_s: float
    end_time_s: float
    duration_s: float
    start_phase: str
    end_phase: str
    trajectory: TransportTrajectory = field(repr=False, compare=False)


@dataclass(frozen=True)
class CommunicationDemand:
    demand_id: str
    outage_id: str
    transport_uav_id: str
    sortie_id: str
    sample_time_s: float
    interval_start_s: float
    interval_end_s: float
    duration_weight_s: float
    phase: str
    point: communication.Point3D
    segment_origin: str = ""
    segment_destination: str = ""
    phase_start_time_s: float = math.nan
    phase_end_time_s: float = math.nan
    transport_uav_type: str = ""


@dataclass
class Step3Result:
    trajectories: Tuple[TransportTrajectory, ...]
    link_samples: Tuple[DirectLinkSample, ...]
    outages: Tuple[OutageInterval, ...]
    demands: Tuple[CommunicationDemand, ...]
    runtime_s: float


def _to_problem2_node(node_id: str) -> str:
    node = str(node_id).strip().upper()
    if node == p2_core.DEPOT:
        return node
    return f"S{int(node[1:]):03d}"


def _to_segment_node(node_id: str) -> str:
    return p2_core.to_segment_node(node_id)


def load_nodes() -> Dict[str, Node3D]:
    """复用问题一节点读取，确保作业海拔使用Excel而不是DEM。"""
    nodes = p1_segment.load_nodes(NODE_FILE)
    nodes = p1_segment.calculate_node_operation_altitudes(nodes)
    result: Dict[str, Node3D] = {}
    for row in nodes.itertuples(index=False):
        node_id = _to_problem2_node(row.node_id)
        result[node_id] = Node3D(
            node_id=node_id,
            longitude_deg=float(row.longitude),
            latitude_deg=float(row.latitude),
            ground_elevation_m=float(row.ground_elevation_m),
            operation_altitude_m=float(row.operation_altitude_m),
        )
    return result


def load_segment_altitudes() -> Tuple[pd.DataFrame, pd.DataFrame]:
    """复用问题一已验证的DEM航段最高高程与计划巡航海拔。"""
    maximum_ground = pd.read_excel(
        paths.require_file(SEGMENT_FILE), sheet_name="max_ground_elevation_matrix", index_col=0
    ).astype(float)
    cruise_altitude = pd.read_excel(
        paths.require_file(SEGMENT_FILE), sheet_name="cruise_altitude_matrix", index_col=0
    ).astype(float)
    maximum_ground.index = maximum_ground.index.astype(str)
    maximum_ground.columns = maximum_ground.columns.astype(str)
    cruise_altitude.index = cruise_altitude.index.astype(str)
    cruise_altitude.columns = cruise_altitude.columns.astype(str)
    mask = ~np.eye(len(cruise_altitude), dtype=bool)
    if not np.allclose(
        cruise_altitude.to_numpy()[mask],
        maximum_ground.to_numpy()[mask] + 50.0,
    ):
        raise RuntimeError("问题一航段预处理不满足巡航海拔=DEM最高高程+50m")
    return maximum_ground, cruise_altitude


def _parse_route(route_text: str) -> Tuple[str, ...]:
    nodes = tuple(part.strip() for part in str(route_text).split("->"))
    if len(nodes) < 3 or nodes[0] != p2_core.DEPOT or nodes[-1] != p2_core.DEPOT:
        raise ValueError(f"无效的问题二运输路径: {route_text}")
    return tuple(_to_problem2_node(node) for node in nodes[1:-1])


def load_problem2_transport_plan(
    data: p2_core.ProblemData,
    result_file: Path = PROBLEM2_RESULT_FILE,
) -> Tuple[Tuple[TransportSortieRecord, ...], pd.DataFrame]:
    """读取当前正式问题二结果，不重新运行运输求解器。"""
    summary = pd.read_excel(paths.require_file(result_file), sheet_name="Summary")
    if summary.empty or str(summary.iloc[0]["Feasibility"]) != "16/16 PASS":
        raise RuntimeError("问题二输入结果没有通过16/16可行性检查")
    sorties = pd.read_excel(paths.require_file(result_file), sheet_name="Sorties")
    uav_schedule = pd.read_excel(paths.require_file(result_file), sheet_name="UAV_Schedule")
    delivery = pd.read_excel(paths.require_file(result_file), sheet_name="Box_Delivery")
    if len(sorties) != 20:
        raise RuntimeError(f"正式问题二结果应有20个架次，实际为{len(sorties)}")

    duration_by_sortie = {
        str(row.Sortie_ID): float(row.Duration_s)
        for row in uav_schedule.itertuples(index=False)
    }
    records: List[TransportSortieRecord] = []
    for row in sorties.itertuples(index=False):
        box_ids = tuple(str(row.Box_IDs).split(";"))
        if any(box_id not in data.boxes for box_id in box_ids):
            raise ValueError(f"架次{row.Sortie_ID}含未知货箱")
        sortie = p2_core.Sortie(
            box_ids=box_ids,
            route=_parse_route(row.Route),
            uav_type=str(row.UAV_Type),
        )
        records.append(
            TransportSortieRecord(
                sortie_id=str(row.Sortie_ID),
                schedule_order=int(row.Schedule_Order),
                sortie=sortie,
                uav_id=str(row.UAV_ID),
                battery_id=str(row.Battery_ID),
                start_time_s=float(row.Start_Time_s),
                takeoff_time_s=float(row.Takeoff_Time_s),
                return_time_s=float(row.Return_Time_s),
                reported_duration_s=duration_by_sortie[str(row.Sortie_ID)],
            )
        )
    return tuple(records), delivery


def _point(node: Node3D, altitude_m: float) -> communication.Point3D:
    return communication.Point3D(
        node.longitude_deg, node.latitude_deg, float(altitude_m)
    )


def build_transport_trajectory(
    record: TransportSortieRecord,
    data: p2_core.ProblemData,
    nodes: Mapping[str, Node3D],
    cruise_altitude: pd.DataFrame,
    delivery_table: pd.DataFrame,
) -> TransportTrajectory:
    """按准备、逐航段三阶段飞行、交接和返航展开连续轨迹。"""
    sortie = record.sortie
    parameters = data.uav_types[sortie.uav_type]
    metrics = p2_core.get_sortie_metrics(sortie, data)
    zone_boxes: Dict[str, List[str]] = {}
    for box_id in sortie.box_ids:
        zone = data.boxes[box_id].service_zone
        zone_boxes.setdefault(zone, []).append(box_id)

    phases: List[TrajectoryPhase] = []
    phase_number = 0

    def append_phase(
        phase: str,
        leg_index: int,
        start_node: str,
        end_node: str,
        start_time_s: float,
        end_time_s: float,
        start_point: communication.Point3D,
        end_point: communication.Point3D,
        horizontal_distance_m: float = 0.0,
        forward_azimuth_deg: float = 0.0,
        planned_cruise_altitude_m: Optional[float] = None,
    ) -> None:
        nonlocal phase_number
        phase_number += 1
        phases.append(
            TrajectoryPhase(
                phase_id=f"{record.sortie_id}-P{phase_number:03d}",
                sortie_id=record.sortie_id,
                transport_uav_id=record.uav_id,
                phase=phase,
                leg_index=leg_index,
                start_node=start_node,
                end_node=end_node,
                start_time_s=float(start_time_s),
                end_time_s=float(end_time_s),
                start_point=start_point,
                end_point=end_point,
                horizontal_distance_m=float(horizontal_distance_m),
                forward_azimuth_deg=float(forward_azimuth_deg),
                cruise_altitude_m=planned_cruise_altitude_m,
            )
        )

    current_time = record.start_time_s
    depot = nodes[p2_core.DEPOT]
    append_phase(
        PREPARE_LOAD,
        0,
        p2_core.DEPOT,
        p2_core.DEPOT,
        current_time,
        record.takeoff_time_s,
        _point(depot, depot.operation_altitude_m),
        _point(depot, depot.operation_altitude_m),
    )
    current_time = record.takeoff_time_s
    current_node_id = p2_core.DEPOT
    service_arrivals: Dict[str, float] = {}
    handover_ends: Dict[str, float] = {}
    box_deliveries: Dict[str, float] = {}
    route_with_return = (*sortie.route, p2_core.DEPOT)

    for leg_index, destination_id in enumerate(route_with_return, start=1):
        start_node = nodes[current_node_id]
        end_node = nodes[destination_id]
        segment_start = _to_segment_node(current_node_id)
        segment_end = _to_segment_node(destination_id)
        cruise_m = float(cruise_altitude.at[segment_start, segment_end])
        climb_m = float(data.climb.at[segment_start, segment_end])
        distance_m = float(data.distance.at[segment_start, segment_end])
        descent_m = float(data.descent.at[segment_start, segment_end])
        azimuth, _, geodesic_distance_m = p1_segment.GEOD.inv(
            start_node.longitude_deg,
            start_node.latitude_deg,
            end_node.longitude_deg,
            end_node.latitude_deg,
        )
        if abs(float(geodesic_distance_m) - distance_m) > DISTANCE_TOLERANCE_M:
            raise RuntimeError(
                f"{current_node_id}->{destination_id}的WGS84距离与问题一矩阵不一致"
            )
        if not math.isclose(
            cruise_m - start_node.operation_altitude_m,
            climb_m,
            abs_tol=1e-6,
        ) or not math.isclose(
            cruise_m - end_node.operation_altitude_m,
            descent_m,
            abs_tol=1e-6,
        ):
            raise RuntimeError("爬升/下降高度与Excel节点作业海拔口径不一致")

        climb_end = current_time + climb_m / parameters.climb_speed_m_s
        append_phase(
            CLIMB,
            leg_index,
            current_node_id,
            destination_id,
            current_time,
            climb_end,
            _point(start_node, start_node.operation_altitude_m),
            _point(start_node, cruise_m),
            planned_cruise_altitude_m=cruise_m,
        )

        cruise_end = climb_end + distance_m / parameters.cruise_speed_m_s
        append_phase(
            CRUISE,
            leg_index,
            current_node_id,
            destination_id,
            climb_end,
            cruise_end,
            _point(start_node, cruise_m),
            _point(end_node, cruise_m),
            horizontal_distance_m=float(geodesic_distance_m),
            forward_azimuth_deg=float(azimuth),
            planned_cruise_altitude_m=cruise_m,
        )

        descent_end = cruise_end + descent_m / parameters.descent_speed_m_s
        append_phase(
            DESCENT,
            leg_index,
            current_node_id,
            destination_id,
            cruise_end,
            descent_end,
            _point(end_node, cruise_m),
            _point(end_node, end_node.operation_altitude_m),
            planned_cruise_altitude_m=cruise_m,
        )
        current_time = descent_end

        if destination_id != p2_core.DEPOT:
            service_arrivals[destination_id] = current_time
            box_count = len(zone_boxes[destination_id])
            handover_duration = (
                parameters.handover_base_time_s
                + box_count * parameters.handover_time_per_box_s
            )
            handover_end = current_time + handover_duration
            append_phase(
                HANDOVER,
                leg_index,
                destination_id,
                destination_id,
                current_time,
                handover_end,
                _point(end_node, end_node.operation_altitude_m),
                _point(end_node, end_node.operation_altitude_m),
            )
            handover_ends[destination_id] = handover_end
            for box_id in zone_boxes[destination_id]:
                box_deliveries[box_id] = handover_end
            current_time = handover_end
        current_node_id = destination_id

    errors = [
        abs(phases[0].start_time_s - record.start_time_s),
        abs(phases[0].end_time_s - record.takeoff_time_s),
        abs(phases[-1].end_time_s - record.return_time_s),
        abs((phases[-1].end_time_s - phases[0].start_time_s) - record.reported_duration_s),
        abs(metrics.duration_s - record.reported_duration_s),
    ]
    expected_arrivals = dict(metrics.arrival_relative_s)
    for zone, arrival_s in service_arrivals.items():
        errors.append(abs(arrival_s - (record.start_time_s + expected_arrivals[zone])))

    expected_deliveries = (
        delivery_table.loc[delivery_table["Sortie_ID"].eq(record.sortie_id)]
        .set_index("Box_ID")["Delivery_Time_s"]
        .astype(float)
        .to_dict()
    )
    if set(expected_deliveries) != set(sortie.box_ids):
        raise RuntimeError(f"架次{record.sortie_id}逐箱结果与架次货箱集合不一致")
    for box_id, delivery_s in box_deliveries.items():
        errors.append(abs(delivery_s - expected_deliveries[box_id]))
        errors.append(
            abs(delivery_s - (record.start_time_s + metrics.delivery_map[box_id]))
        )

    maximum_error = max(errors, default=0.0)
    if maximum_error > TIME_TOLERANCE_S:
        raise RuntimeError(
            f"架次{record.sortie_id}轨迹时刻与问题二结果不一致，"
            f"最大误差={maximum_error:.12g}s"
        )
    return TransportTrajectory(
        record=record,
        phases=tuple(phases),
        service_arrival_times_s=service_arrivals,
        service_handover_end_times_s=handover_ends,
        box_delivery_times_s=box_deliveries,
        maximum_time_error_s=maximum_error,
    )


def build_all_transport_trajectories(
    records: Sequence[TransportSortieRecord],
    data: p2_core.ProblemData,
    nodes: Mapping[str, Node3D],
    cruise_altitude: pd.DataFrame,
    delivery_table: pd.DataFrame,
) -> Tuple[TransportTrajectory, ...]:
    trajectories = tuple(
        build_transport_trajectory(
            record, data, nodes, cruise_altitude, delivery_table
        )
        for record in records
    )
    if len(trajectories) != 20:
        raise RuntimeError("未完整重建20个运输架次")
    return trajectories


class DirectLinkEvaluator:
    """单架次A_uG(t)评价器，缓存二分细化产生的重复时刻与位置。"""

    def __init__(
        self,
        trajectory: TransportTrajectory,
        gateway: communication.GatewayEndpoint,
        parameters: communication.CommunicationParameters,
        dem: communication.DemData,
        shared_position_cache: Dict[Tuple[float, float, float], communication.LinkEvaluation],
    ) -> None:
        self.trajectory = trajectory
        self.gateway = gateway
        self.parameters = parameters
        self.dem = dem
        self.shared_position_cache = shared_position_cache
        self.time_cache: Dict[float, DirectLinkSample] = {}
        self.boundaries = {
            phase.start_time_s for phase in trajectory.communication_phases
        } | {phase.end_time_s for phase in trajectory.communication_phases}

    @staticmethod
    def _position_key(point: communication.Point3D) -> Tuple[float, float, float]:
        return (
            round(point.longitude_deg, POSITION_CACHE_DECIMALS[0]),
            round(point.latitude_deg, POSITION_CACHE_DECIMALS[1]),
            round(point.altitude_m, POSITION_CACHE_DECIMALS[2]),
        )

    def sample(self, time_s: float, refined: bool = False) -> DirectLinkSample:
        key = float(time_s)
        cached = self.time_cache.get(key)
        if cached is not None:
            if refined and not cached.is_refined_transition:
                cached = DirectLinkSample(
                    **{
                        **cached.__dict__,
                        "is_refined_transition": True,
                    }
                )
                self.time_cache[key] = cached
            return cached

        phase = self.trajectory.phase_at(key)
        point = phase.position_at(key)
        position_key = self._position_key(point)
        link = self.shared_position_cache.get(position_key)
        if link is None:
            link = communication.evaluate_ug_link(
                point, self.gateway.point, self.parameters, self.dem
            )
            self.shared_position_cache[position_key] = link
        sample = DirectLinkSample(
            time_s=key,
            transport_uav_id=self.trajectory.record.uav_id,
            sortie_id=self.trajectory.record.sortie_id,
            phase=phase.phase,
            longitude_deg=point.longitude_deg,
            latitude_deg=point.latitude_deg,
            altitude_m=point.altitude_m,
            distance_to_g01_3d_m=link.distance_3d_m,
            terrain_obstruction_b=int(link.obstructed),
            fspl_db=link.fspl_db,
            path_loss_db=link.total_path_loss_db,
            maximum_path_loss_db=link.maximum_path_loss_db,
            a_ug=int(link.available),
            is_stage_boundary=any(
                abs(key - boundary) <= TIME_TOLERANCE_S
                for boundary in self.boundaries
            ),
            is_refined_transition=refined,
        )
        self.time_cache[key] = sample
        return sample

    def state(self, time_s: float) -> int:
        return self.sample(time_s).a_ug


def check_direct_communication(
    trajectories: Sequence[TransportTrajectory],
    gateway: communication.GatewayEndpoint,
    parameters: communication.CommunicationParameters,
    dem: communication.DemData,
    shared_position_cache: Optional[
        Dict[Tuple[float, float, float], communication.LinkEvaluation]
    ] = None,
) -> Tuple[Tuple[DirectLinkSample, ...], Tuple[OutageInterval, ...]]:
    """检查 UG；可选共享缓存仅复用同一三维端点的确定性链路结果。"""
    all_samples: List[DirectLinkSample] = []
    outages: List[OutageInterval] = []
    if shared_position_cache is None:
        shared_position_cache = {}

    for trajectory in trajectories:
        evaluator = DirectLinkEvaluator(
            trajectory, gateway, parameters, dem, shared_position_cache
        )
        boundaries = sorted(evaluator.boundaries)
        grid = communication.deterministic_time_grid(
            trajectory.communication_start_s,
            trajectory.communication_end_s,
            boundaries,
            communication.COMMUNICATION_TIME_STEP_S,
        )
        coarse = [evaluator.sample(time_s) for time_s in grid]
        transitions: List[Tuple[float, int, int]] = []
        for left, right in zip(coarse, coarse[1:]):
            if left.a_ug == right.a_ug:
                continue
            transition_time = communication.refine_binary_state_transition(
                evaluator.state,
                left.time_s,
                right.time_s,
                communication.STATE_TRANSITION_TOLERANCE_S,
            )
            evaluator.sample(transition_time, refined=True)
            transitions.append((transition_time, left.a_ug, right.a_ug))

        current_outage_start: Optional[float] = (
            trajectory.communication_start_s if coarse[0].a_ug == 0 else None
        )
        for transition_time, old_state, new_state in transitions:
            if old_state == 1 and new_state == 0:
                current_outage_start = transition_time
            elif old_state == 0 and new_state == 1:
                if current_outage_start is None:
                    raise RuntimeError("通信状态切换序列不一致")
                end_time = transition_time
                start_probe = min(
                    end_time,
                    current_outage_start
                    + communication.STATE_TRANSITION_TOLERANCE_S,
                )
                end_probe = max(
                    current_outage_start,
                    end_time - communication.STATE_TRANSITION_TOLERANCE_S,
                )
                outages.append(
                    OutageInterval(
                        outage_id="",
                        transport_uav_id=trajectory.record.uav_id,
                        sortie_id=trajectory.record.sortie_id,
                        start_time_s=current_outage_start,
                        end_time_s=end_time,
                        duration_s=end_time - current_outage_start,
                        start_phase=trajectory.phase_at(start_probe).phase,
                        end_phase=trajectory.phase_at(end_probe).phase,
                        trajectory=trajectory,
                    )
                )
                current_outage_start = None
        if current_outage_start is not None:
            end_time = trajectory.communication_end_s
            start_probe = min(
                end_time,
                current_outage_start + communication.STATE_TRANSITION_TOLERANCE_S,
            )
            outages.append(
                OutageInterval(
                    outage_id="",
                    transport_uav_id=trajectory.record.uav_id,
                    sortie_id=trajectory.record.sortie_id,
                    start_time_s=current_outage_start,
                    end_time_s=end_time,
                    duration_s=end_time - current_outage_start,
                    start_phase=trajectory.phase_at(start_probe).phase,
                    end_phase=trajectory.phase_at(
                        max(current_outage_start, end_time - TIME_TOLERANCE_S)
                    ).phase,
                    trajectory=trajectory,
                )
            )
        all_samples.extend(sorted(evaluator.time_cache.values(), key=lambda row: row.time_s))

    numbered_outages = tuple(
        OutageInterval(
            outage_id=f"OUT-{index:04d}",
            transport_uav_id=outage.transport_uav_id,
            sortie_id=outage.sortie_id,
            start_time_s=outage.start_time_s,
            end_time_s=outage.end_time_s,
            duration_s=outage.duration_s,
            start_phase=outage.start_phase,
            end_phase=outage.end_phase,
            trajectory=outage.trajectory,
        )
        for index, outage in enumerate(
            sorted(outages, key=lambda row: (row.start_time_s, row.sortie_id)),
            start=1,
        )
    )
    return tuple(all_samples), numbered_outages


def build_outage_demands(
    outages: Sequence[OutageInterval],
    step_s: float = communication.COMMUNICATION_TIME_STEP_S,
) -> Tuple[CommunicationDemand, ...]:
    """把中断区间离散为带时长权重的q=(transport_uav,time)需求。"""
    demands: List[CommunicationDemand] = []
    for outage in outages:
        interval_start = outage.start_time_s
        while interval_start < outage.end_time_s - TIME_TOLERANCE_S:
            interval_end = min(outage.end_time_s, interval_start + step_s)
            sample_time = (interval_start + interval_end) / 2.0
            phase = outage.trajectory.phase_at(sample_time)
            point = phase.position_at(sample_time)
            demands.append(
                CommunicationDemand(
                    demand_id=f"Q-{len(demands) + 1:06d}",
                    outage_id=outage.outage_id,
                    transport_uav_id=outage.transport_uav_id,
                    sortie_id=outage.sortie_id,
                    sample_time_s=sample_time,
                    interval_start_s=interval_start,
                    interval_end_s=interval_end,
                    duration_weight_s=interval_end - interval_start,
                    phase=phase.phase,
                    point=point,
                    segment_origin=phase.start_node,
                    segment_destination=phase.end_node,
                    phase_start_time_s=phase.start_time_s,
                    phase_end_time_s=phase.end_time_s,
                    transport_uav_type=outage.trajectory.record.sortie.uav_type,
                )
            )
            interval_start = interval_end
    return tuple(demands)


def _transport_sorties_table(
    trajectories: Sequence[TransportTrajectory],
) -> pd.DataFrame:
    rows = []
    for trajectory in trajectories:
        record = trajectory.record
        rows.append(
            {
                "Sortie_ID": record.sortie_id,
                "Schedule_Order": record.schedule_order,
                "UAV_ID": record.uav_id,
                "UAV_Type": record.sortie.uav_type,
                "Battery_ID": record.battery_id,
                "Start_Time_s": record.start_time_s,
                "Takeoff_Time_s": record.takeoff_time_s,
                "Communication_Check_Start_s": trajectory.communication_start_s,
                "Return_Time_s": record.return_time_s,
                "Rebuilt_Return_Time_s": trajectory.phases[-1].end_time_s,
                "Reported_Duration_s": record.reported_duration_s,
                "Rebuilt_Duration_s": trajectory.total_duration_s,
                "Maximum_Time_Error_s": trajectory.maximum_time_error_s,
                "Route": " -> ".join(
                    (p2_core.DEPOT, *record.sortie.route, p2_core.DEPOT)
                ),
                "Box_Count": len(record.sortie.box_ids),
                "Time_Consistency": "PASS",
            }
        )
    return pd.DataFrame(rows).sort_values("Schedule_Order")


def _trajectory_phases_table(
    trajectories: Sequence[TransportTrajectory],
) -> pd.DataFrame:
    rows = []
    for trajectory in trajectories:
        for phase in trajectory.phases:
            rows.append(
                {
                    "Phase_ID": phase.phase_id,
                    "Sortie_ID": phase.sortie_id,
                    "UAV_ID": phase.transport_uav_id,
                    "Phase": phase.phase,
                    "Communication_Required": phase.communication_required,
                    "Leg_Index": phase.leg_index,
                    "Start_Node": phase.start_node,
                    "End_Node": phase.end_node,
                    "Start_Time_s": phase.start_time_s,
                    "End_Time_s": phase.end_time_s,
                    "Duration_s": phase.duration_s,
                    "Start_Longitude": phase.start_point.longitude_deg,
                    "Start_Latitude": phase.start_point.latitude_deg,
                    "Start_Altitude_m": phase.start_point.altitude_m,
                    "End_Longitude": phase.end_point.longitude_deg,
                    "End_Latitude": phase.end_point.latitude_deg,
                    "End_Altitude_m": phase.end_point.altitude_m,
                    "Cruise_Altitude_m": phase.cruise_altitude_m,
                }
            )
    return pd.DataFrame(rows)


def _direct_link_table(samples: Sequence[DirectLinkSample]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Time_s": row.time_s,
                "UAV_ID": row.transport_uav_id,
                "Sortie_ID": row.sortie_id,
                "Phase": row.phase,
                "Longitude": row.longitude_deg,
                "Latitude": row.latitude_deg,
                "Absolute_Altitude_m": row.altitude_m,
                "Distance_to_G01_3D_m": row.distance_to_g01_3d_m,
                "Terrain_Obstruction_b": row.terrain_obstruction_b,
                "L_FSPL_dB": row.fspl_db,
                "L_path_dB": row.path_loss_db,
                "Lmax_UG_dB": row.maximum_path_loss_db,
                "A_uG": row.a_ug,
                "Is_Stage_Boundary": row.is_stage_boundary,
                "Is_Refined_Transition": row.is_refined_transition,
            }
            for row in samples
        ]
    ).sort_values(["Sortie_ID", "Time_s"])


def _outage_table(outages: Sequence[OutageInterval]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Outage_ID": row.outage_id,
                "Transport_UAV_ID": row.transport_uav_id,
                "Sortie_ID": row.sortie_id,
                "Start_Time_s": row.start_time_s,
                "End_Time_s": row.end_time_s,
                "Duration_s": row.duration_s,
                "Start_Phase": row.start_phase,
                "End_Phase": row.end_phase,
            }
            for row in outages
        ],
        columns=[
            "Outage_ID",
            "Transport_UAV_ID",
            "Sortie_ID",
            "Start_Time_s",
            "End_Time_s",
            "Duration_s",
            "Start_Phase",
            "End_Phase",
        ],
    )


def _demand_table(demands: Sequence[CommunicationDemand]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Demand_ID": row.demand_id,
                "Outage_ID": row.outage_id,
                "Transport_UAV_ID": row.transport_uav_id,
                "Sortie_ID": row.sortie_id,
                "Sample_Time_s": row.sample_time_s,
                "Interval_Start_s": row.interval_start_s,
                "Interval_End_s": row.interval_end_s,
                "Duration_Weight_s": row.duration_weight_s,
                "Phase": row.phase,
                "Longitude": row.point.longitude_deg,
                "Latitude": row.point.latitude_deg,
                "Absolute_Altitude_m": row.point.altitude_m,
            }
            for row in demands
        ]
    )


def export_step3(result: Step3Result, output_file: Path = OUTPUT_FILE) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    tables = {
        "Transport_Sorties": _transport_sorties_table(result.trajectories),
        "Trajectory_Phases": _trajectory_phases_table(result.trajectories),
        "Direct_Link_Check": _direct_link_table(result.link_samples),
        "Outage_Intervals": _outage_table(result.outages),
        "Outage_Demands": _demand_table(result.demands),
    }
    paths.ensure_output_path(output_file)
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        for sheet_name, table in tables.items():
            table.to_excel(writer, sheet_name=sheet_name, index=False)
        for worksheet in writer.book.worksheets:
            worksheet.freeze_panes = "A2"
            worksheet.auto_filter.ref = worksheet.dimensions
            for column in worksheet.columns:
                values = ["" if cell.value is None else str(cell.value) for cell in column[:1000]]
                width = min(max(max(map(len, values), default=10) + 2, 12), 32)
                worksheet.column_dimensions[column[0].column_letter].width = width
                for cell in column[1:]:
                    if isinstance(cell.value, float):
                        cell.number_format = "0.000000000"


def run_step3(export: bool = True) -> Step3Result:
    started = perf_counter()
    data = p2_core.load_data()
    nodes = load_nodes()
    _, cruise_altitude = load_segment_altitudes()
    records, delivery_table = load_problem2_transport_plan(data)
    trajectories = build_all_transport_trajectories(
        records, data, nodes, cruise_altitude, delivery_table
    )
    maximum_error = max(row.maximum_time_error_s for row in trajectories)
    print(
        f"Transport trajectory reconstruction: 20/20 PASS, "
        f"max_time_error={maximum_error:.12g}s",
        flush=True,
    )

    parameters = communication.load_communication_parameters()
    gateway = communication.load_g01_endpoint(parameters)
    dem = communication.load_dem()
    link_samples, outages = check_direct_communication(
        trajectories, gateway, parameters, dem
    )
    demands = build_outage_demands(outages)
    runtime_s = perf_counter() - started
    result = Step3Result(
        trajectories=trajectories,
        link_samples=link_samples,
        outages=outages,
        demands=demands,
        runtime_s=runtime_s,
    )
    if export:
        export_step3(result)

    checked_duration = sum(
        trajectory.communication_end_s - trajectory.communication_start_s
        for trajectory in trajectories
    )
    affected_sorties = len({row.sortie_id for row in outages})
    total_outage = sum(row.duration_s for row in outages)
    longest_outage = max((row.duration_s for row in outages), default=0.0)
    print(f"Transport sorties = {len(trajectories)}", flush=True)
    print(f"Communication checked duration = {checked_duration:.6f} s", flush=True)
    print(f"Sorties with direct-link outage = {affected_sorties}", flush=True)
    print(f"Outage intervals = {len(outages)}", flush=True)
    print(f"Cumulative outage duration = {total_outage:.6f} s", flush=True)
    print(f"Longest outage duration = {longest_outage:.6f} s", flush=True)
    for outage in outages:
        print(
            f"{outage.outage_id}: UAV={outage.transport_uav_id}, "
            f"sortie={outage.sortie_id}, "
            f"[{outage.start_time_s:.3f}, {outage.end_time_s:.3f}], "
            f"duration={outage.duration_s:.3f}s, "
            f"phase={outage.start_phase}->{outage.end_phase}",
            flush=True,
        )
    print(f"Runtime = {runtime_s:.6f} s", flush=True)
    if export:
        print(f"Result = {OUTPUT_FILE}", flush=True)
    return result


if __name__ == "__main__":
    run_step3(export=True)
