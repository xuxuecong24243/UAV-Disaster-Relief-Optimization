"""Problem 3 Phase A.5：固定运输航段的 UG/UR 通信几何缓存。

缓存只改变确定性通信几何的复用方式，不改变链路公式、DEM LOS、时间精度、
elite pool、目标函数或可行性定义。所有状态转换以航段阶段空间进度 q 表示，
实际 UAV 机型只把 q 映射为相对阶段时间。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
import argparse
from bisect import bisect_right
import gzip
import json
import math
import pickle
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
import problem3_communication as communication  # noqa: E402
import problem3_core as core  # noqa: E402
import problem3_relay as relay  # noqa: E402


CACHE_VERSION = 1
CACHE_FILE = paths.output_file("problem3", "problem3_communication_geometry_cache.pkl.gz")
MANIFEST_FILE = (
    paths.output_file("problem3", "problem3_communication_geometry_cache_manifest.xlsx")
)
PHASES = (core.CLIMB, core.CRUISE, core.DESCENT)


@dataclass(frozen=True)
class BinaryTransitionProfile:
    """q=0 的状态及之后每次0/1翻转的 q 边界。"""

    initial_available: bool
    transition_q: Tuple[float, ...]

    def available_at(self, q: float) -> bool:
        value = min(1.0, max(0.0, float(q)))
        transitions_before = bisect_right(self.transition_q, value)
        return bool(self.initial_available) ^ bool(transitions_before % 2)

    def intervals(self) -> Tuple[Tuple[float, float, bool], ...]:
        boundaries = (0.0, *self.transition_q, 1.0)
        state = bool(self.initial_available)
        rows = []
        for left, right in zip(boundaries, boundaries[1:]):
            rows.append((float(left), float(right), state))
            state = not state
        return tuple(rows)


@dataclass(frozen=True)
class SegmentGeometry:
    segment_id: str
    origin: str
    destination: str
    horizontal_distance_m: float
    flight_altitude_m: float
    climb_height_m: float
    descent_height_m: float
    phase_durations_by_uav: Mapping[str, Tuple[float, float, float]]


@dataclass(frozen=True)
class SegmentCommunicationProfile:
    geometry: SegmentGeometry
    ug_by_phase: Mapping[str, BinaryTransitionProfile]
    ur_by_phase: Mapping[str, Tuple[BinaryTransitionProfile, ...]]


@dataclass(frozen=True)
class CommunicationGeometryCache:
    version: int
    signature: str
    candidate_ids: Tuple[str, ...]
    segment_profiles: Mapping[str, SegmentCommunicationProfile]
    ug_handover: Mapping[str, bool]
    ur_handover: Mapping[str, Tuple[bool, ...]]
    actual_directed_segment_count: int
    preprocessing_time_s: float
    coarse_state_count: int
    ur_transition_refinement_calls: int

    def segment(self, origin: str, destination: str) -> SegmentCommunicationProfile:
        return self.segment_profiles[segment_id(origin, destination)]

    def candidate_index(self) -> Mapping[str, int]:
        return {value: index for index, value in enumerate(self.candidate_ids)}


class LazyPersistentCommunicationCache:
    """按需计算单条有向航段，并在每次 miss 后立即原子持久化。"""

    def __init__(
        self,
        context: object,
        candidates: Sequence[relay.RelayCandidate],
        path: Path = CACHE_FILE,
        manifest_path: Path = MANIFEST_FILE,
        worker_count: int = relay.DEFAULT_WORKER_COUNT,
    ) -> None:
        self.context = context
        self.candidates = tuple(candidates)
        self.path = path
        self.manifest_path = manifest_path
        self.worker_count = worker_count
        self.universe = actual_possible_directed_segments(
            context.transport_data
        )
        self.universe_set = set(self.universe)
        self.expected_signature = cache_signature(
            context, self.candidates, self.universe
        )
        self.hits = 0
        self.misses = 0
        self.new_segments_computed = 0
        self.load_time_s = 0.0
        load_started = perf_counter()
        if not path.exists() and paths.cache_input_file().exists():
            paths.prepare_cache(path)
        if path.exists():
            value = load_cache(path)
            if value.signature != self.expected_signature:
                raise ValueError(
                    "lazy communication cache签名与当前数据/elite pool不一致"
                )
            if value.candidate_ids != tuple(
                row.candidate_id for row in self.candidates
            ):
                raise ValueError("lazy communication cache候选顺序不一致")
            self.cache = value
        else:
            # 空航段构建只预计算15个HANDOVER点；正式航段均保持lazy。
            self.cache = build_geometry_cache(
                context,
                self.candidates,
                tuple(),
                worker_count=worker_count,
                progress=False,
                signature_segments=self.universe,
            )
            self._persist()
        self.load_time_s = perf_counter() - load_started

    def ensure(
        self, origin: str, destination: str
    ) -> SegmentCommunicationProfile:
        key = (origin, destination)
        if key not in self.universe_set:
            raise KeyError(f"航段{origin}->{destination}不在实际可行航段集合中")
        identifier = segment_id(origin, destination)
        existing = self.cache.segment_profiles.get(identifier)
        if existing is not None:
            self.hits += 1
            return existing
        self.misses += 1
        partial = build_geometry_cache(
            self.context,
            self.candidates,
            (key,),
            worker_count=self.worker_count,
            progress=False,
            signature_segments=self.universe,
            include_handover=False,
        )
        profiles = dict(self.cache.segment_profiles)
        profiles[identifier] = partial.segment_profiles[identifier]
        self.cache = replace(
            self.cache,
            segment_profiles=profiles,
            preprocessing_time_s=(
                self.cache.preprocessing_time_s + partial.preprocessing_time_s
            ),
            coarse_state_count=(
                self.cache.coarse_state_count + partial.coarse_state_count
            ),
            ur_transition_refinement_calls=(
                self.cache.ur_transition_refinement_calls
                + partial.ur_transition_refinement_calls
            ),
        )
        self.new_segments_computed += 1
        self._persist()
        return self.cache.segment_profiles[identifier]

    def prewarm(
        self, segments: Iterable[Tuple[str, str]], progress: bool = True
    ) -> Tuple[int, float]:
        unique = tuple(sorted(set(segments)))
        before = self.new_segments_computed
        started = perf_counter()
        for index, pair in enumerate(unique, start=1):
            self.ensure(*pair)
            if progress:
                print(
                    f"lazy prewarm segments {index}/{len(unique)}; "
                    f"cached={len(self.cache.segment_profiles)}",
                    flush=True,
                )
        return self.new_segments_computed - before, perf_counter() - started

    def snapshot(self) -> Tuple[int, int, int]:
        return self.new_segments_computed, self.hits, self.misses

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        save_cache(self.cache, temporary)
        temporary.replace(self.path)
        paths.register_generated_cache(self.path)
        export_manifest(
            self.cache, self.path.stat().st_size, self.manifest_path
        )


def segment_id(origin: str, destination: str) -> str:
    return f"{origin}->{destination}"


def actual_possible_directed_segments(
    data: p2_core.ProblemData,
) -> Tuple[Tuple[str, str], ...]:
    """枚举至少存在一个两区可行箱组的服务区相邻有向航段。"""
    zones = tuple(sorted({box.service_zone for box in data.boxes.values()}))
    boxes_by_zone = {
        zone: tuple(
            box.box_id for box in data.boxes.values() if box.service_zone == zone
        )
        for zone in zones
    }
    service_pairs: List[Tuple[str, str]] = []
    for origin in zones:
        for destination in zones:
            if origin == destination:
                continue
            feasible = False
            for first in boxes_by_zone[origin]:
                for second in boxes_by_zone[destination]:
                    box_ids = tuple(sorted((first, second)))
                    for uav_type in p2_core.UAV_TYPES:
                        sortie = p2_core.Sortie(
                            box_ids, (origin, destination), uav_type
                        )
                        if p2_core.check_sortie_feasibility(sortie, data):
                            feasible = True
                            break
                    if feasible:
                        break
                if feasible:
                    break
            if feasible:
                service_pairs.append((origin, destination))
    depot_pairs = [
        pair
        for zone in zones
        for pair in ((p2_core.DEPOT, zone), (zone, p2_core.DEPOT))
    ]
    return tuple(sorted((*depot_pairs, *service_pairs)))


def _segment_geometry(
    origin: str,
    destination: str,
    context: object,
) -> SegmentGeometry:
    data = context.transport_data
    nodes = context.nodes
    start = nodes[origin]
    end = nodes[destination]
    matrix_origin = core._to_segment_node(origin)
    matrix_destination = core._to_segment_node(destination)
    distance = float(data.distance.at[matrix_origin, matrix_destination])
    flight = float(context.cruise_altitude.at[matrix_origin, matrix_destination])
    climb = flight - start.operation_altitude_m
    descent = flight - end.operation_altitude_m
    durations = {
        uav_type: (
            climb / parameters.climb_speed_m_s,
            distance / parameters.cruise_speed_m_s,
            descent / parameters.descent_speed_m_s,
        )
        for uav_type, parameters in data.uav_types.items()
    }
    return SegmentGeometry(
        segment_id=segment_id(origin, destination),
        origin=origin,
        destination=destination,
        horizontal_distance_m=distance,
        flight_altitude_m=flight,
        climb_height_m=climb,
        descent_height_m=descent,
        phase_durations_by_uav=durations,
    )


def _point_at_q(
    geometry: SegmentGeometry,
    phase: str,
    q: float,
    nodes: Mapping[str, core.Node3D],
) -> communication.Point3D:
    q = min(1.0, max(0.0, float(q)))
    origin = nodes[geometry.origin]
    destination = nodes[geometry.destination]
    if phase == core.CLIMB:
        altitude = origin.operation_altitude_m + geometry.climb_height_m * q
        return communication.Point3D(
            origin.longitude_deg, origin.latitude_deg, altitude
        )
    if phase == core.DESCENT:
        altitude = geometry.flight_altitude_m - geometry.descent_height_m * q
        return communication.Point3D(
            destination.longitude_deg, destination.latitude_deg, altitude
        )
    if phase != core.CRUISE:
        raise ValueError(f"未知航段阶段: {phase}")
    azimuth, _, geodesic_distance = communication.GEOD.inv(
        origin.longitude_deg,
        origin.latitude_deg,
        destination.longitude_deg,
        destination.latitude_deg,
    )
    longitude, latitude, _ = communication.GEOD.fwd(
        origin.longitude_deg,
        origin.latitude_deg,
        azimuth,
        float(geodesic_distance) * q,
    )
    return communication.Point3D(
        float(longitude), float(latitude), geometry.flight_altitude_m
    )


def _coarse_q(geometry: SegmentGeometry, phase_index: int) -> np.ndarray:
    maximum_duration = max(
        durations[phase_index]
        for durations in geometry.phase_durations_by_uav.values()
    )
    if maximum_duration <= core.TIME_TOLERANCE_S:
        return np.asarray((0.0, 1.0), dtype=np.float64)
    count = max(1, int(math.ceil(maximum_duration / communication.COMMUNICATION_TIME_STEP_S)))
    return np.linspace(0.0, 1.0, count + 1, dtype=np.float64)


def _spatial_states(
    points: Sequence[communication.Point3D],
) -> relay.SpatialDemandStates:
    longitude = np.asarray([row.longitude_deg for row in points], dtype=np.float64)
    latitude = np.asarray([row.latitude_deg for row in points], dtype=np.float64)
    altitude = np.asarray([row.altitude_m for row in points], dtype=np.float64)
    x_m, y_m = relay.PROJECT_TO_LOCAL.transform(longitude, latitude)
    count = len(points)
    return relay.SpatialDemandStates(
        longitude_deg=longitude,
        latitude_deg=latitude,
        altitude_m=altitude,
        projected_x_m=np.asarray(x_m, dtype=np.float64),
        projected_y_m=np.asarray(y_m, dtype=np.float64),
        duration_weight_s=np.ones(count, dtype=np.float64),
        demand_count=np.ones(count, dtype=np.int32),
        member_demand_indices=tuple(
            np.asarray((index,), dtype=np.int32) for index in range(count)
        ),
        outage_ids=tuple(frozenset() for _ in range(count)),
        uav_ids=tuple(frozenset() for _ in range(count)),
    )


def _profile_from_samples(
    q_values: np.ndarray,
    states: np.ndarray,
    state_function: object,
    q_tolerance: float,
) -> Tuple[BinaryTransitionProfile, int]:
    transitions: List[float] = []
    calls = 0
    for index in np.flatnonzero(states[:-1] != states[1:]):
        left = float(q_values[int(index)])
        right = float(q_values[int(index) + 1])
        left_state = bool(states[int(index)])
        while right - left > q_tolerance:
            middle = (left + right) / 2.0
            middle_state = bool(state_function(middle))
            calls += 1
            if middle_state == left_state:
                left = middle
            else:
                right = middle
        transitions.append((left + right) / 2.0)
    return BinaryTransitionProfile(bool(states[0]), tuple(transitions)), calls


def cache_signature(
    context: object,
    candidates: Sequence[relay.RelayCandidate],
    segments: Sequence[Tuple[str, str]],
) -> str:
    payload = {
        "version": CACHE_VERSION,
        "time_step_s": communication.COMMUNICATION_TIME_STEP_S,
        "transition_tolerance_s": communication.STATE_TRANSITION_TOLERANCE_S,
        "segments": list(segments),
        "nodes": [
            (
                key,
                row.longitude_deg,
                row.latitude_deg,
                row.operation_altitude_m,
            )
            for key, row in sorted(context.nodes.items())
        ],
        "uav_speeds": [
            (
                key,
                row.cruise_speed_m_s,
                row.climb_speed_m_s,
                row.descent_speed_m_s,
            )
            for key, row in sorted(context.transport_data.uav_types.items())
        ],
        "candidates": [
            (
                row.candidate_id,
                row.longitude_deg,
                row.latitude_deg,
                row.absolute_altitude_m,
            )
            for row in candidates
        ],
        "communication": {
            "frequency": context.communication_parameters.frequency_mhz,
            "system_loss": context.communication_parameters.system_loss_db,
            "obstruction_loss": context.communication_parameters.obstruction_loss_db,
            "sensitivity": context.communication_parameters.receiver_sensitivity_dbm,
            "margin": context.communication_parameters.fade_margin_db,
        },
        "dem": {
            "shape": context.dem.elevation_m.shape,
            "transform": tuple(context.dem.transform),
            "minimum": float(np.nanmin(context.dem.elevation_m)),
            "maximum": float(np.nanmax(context.dem.elevation_m)),
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return sha256(encoded).hexdigest()


def build_geometry_cache(
    context: object,
    candidates: Sequence[relay.RelayCandidate],
    segments: Sequence[Tuple[str, str]],
    worker_count: int = relay.DEFAULT_WORKER_COUNT,
    progress: bool = True,
    signature_segments: Optional[Sequence[Tuple[str, str]]] = None,
    include_handover: bool = True,
) -> CommunicationGeometryCache:
    """批量计算所给有向航段；可传10条航段先做耗时 benchmark。"""
    started = perf_counter()
    geometries = tuple(_segment_geometry(*pair, context) for pair in segments)
    phase_rows: List[Tuple[int, str, np.ndarray, np.ndarray]] = []
    unique_points: List[communication.Point3D] = []
    point_index: Dict[Tuple[float, float, float], int] = {}

    def add_point(point: communication.Point3D) -> int:
        key = (
            round(point.longitude_deg, 10),
            round(point.latitude_deg, 10),
            round(point.altitude_m, 6),
        )
        if key not in point_index:
            point_index[key] = len(unique_points)
            unique_points.append(point)
        return point_index[key]

    for segment_index, geometry in enumerate(geometries):
        for phase_index, phase in enumerate(PHASES):
            q_values = _coarse_q(geometry, phase_index)
            indexes = np.asarray(
                [
                    add_point(_point_at_q(geometry, phase, q, context.nodes))
                    for q in q_values
                ],
                dtype=np.int32,
            )
            phase_rows.append((segment_index, phase, q_values, indexes))

    handover_nodes = (
        tuple(key for key in sorted(context.nodes) if key != p2_core.DEPOT)
        if include_handover
        else tuple()
    )
    handover_point_indexes = {
        node_id: add_point(
            communication.Point3D(
                context.nodes[node_id].longitude_deg,
                context.nodes[node_id].latitude_deg,
                context.nodes[node_id].operation_altitude_m,
            )
        )
        for node_id in handover_nodes
    }
    if progress:
        print(
            f"Phase A.5 coarse states={len(unique_points)}, "
            f"segments={len(segments)}, candidates={len(candidates)}",
            flush=True,
        )
    states = _spatial_states(unique_points)
    relay.precompute_ur_coverage(candidates, states, worker_count=worker_count)
    ur_matrix = np.zeros((len(candidates), len(unique_points)), dtype=bool)
    for candidate_index, candidate in enumerate(candidates):
        ur_matrix[candidate_index, candidate.covered_state_indices] = True
    ug_states = np.asarray(
        [
            communication.evaluate_ug_link(
                point,
                context.gateway.point,
                context.communication_parameters,
                context.dem,
            ).available
            for point in unique_points
        ],
        dtype=bool,
    )

    segment_profiles: Dict[str, SegmentCommunicationProfile] = {}
    refinement_calls = 0
    rows_by_segment: Dict[int, List[Tuple[str, np.ndarray, np.ndarray]]] = {}
    for segment_index, phase, q_values, indexes in phase_rows:
        rows_by_segment.setdefault(segment_index, []).append(
            (phase, q_values, indexes)
        )
    for completed, geometry in enumerate(geometries, start=1):
        ug_by_phase: Dict[str, BinaryTransitionProfile] = {}
        ur_by_phase: Dict[str, Tuple[BinaryTransitionProfile, ...]] = {}
        for phase, q_values, indexes in rows_by_segment[completed - 1]:
            phase_index = PHASES.index(phase)
            maximum_duration = max(
                value[phase_index]
                for value in geometry.phase_durations_by_uav.values()
            )
            q_tolerance = (
                communication.STATE_TRANSITION_TOLERANCE_S
                / max(maximum_duration, communication.STATE_TRANSITION_TOLERANCE_S)
            )

            def ug_state(q: float) -> bool:
                point = _point_at_q(geometry, phase, q, context.nodes)
                return communication.evaluate_ug_link(
                    point,
                    context.gateway.point,
                    context.communication_parameters,
                    context.dem,
                ).available

            ug_profile, calls = _profile_from_samples(
                q_values, ug_states[indexes], ug_state, q_tolerance
            )
            refinement_calls += calls
            ug_by_phase[phase] = ug_profile
            candidate_profiles: List[BinaryTransitionProfile] = []
            for candidate_index, candidate in enumerate(candidates):

                def ur_state(q: float, relay_candidate: relay.RelayCandidate = candidate) -> bool:
                    point = _point_at_q(geometry, phase, q, context.nodes)
                    endpoint = communication.Point3D(
                        relay_candidate.longitude_deg,
                        relay_candidate.latitude_deg,
                        relay_candidate.absolute_altitude_m,
                    )
                    return communication.evaluate_ur_link(
                        point,
                        endpoint,
                        context.communication_parameters,
                        context.dem,
                    ).available

                profile, calls = _profile_from_samples(
                    q_values,
                    ur_matrix[candidate_index, indexes],
                    ur_state,
                    q_tolerance,
                )
                refinement_calls += calls
                candidate_profiles.append(profile)
            ur_by_phase[phase] = tuple(candidate_profiles)
        segment_profiles[geometry.segment_id] = SegmentCommunicationProfile(
            geometry, ug_by_phase, ur_by_phase
        )
        if progress:
            print(
                f"processed segments {completed}/{len(geometries)}; "
                f"refinement_calls={refinement_calls}",
                flush=True,
            )

    ug_handover = {
        node_id: bool(ug_states[index])
        for node_id, index in handover_point_indexes.items()
    }
    ur_handover = {
        node_id: tuple(bool(value) for value in ur_matrix[:, index])
        for node_id, index in handover_point_indexes.items()
    }
    return CommunicationGeometryCache(
        version=CACHE_VERSION,
        signature=cache_signature(
            context,
            candidates,
            segments if signature_segments is None else signature_segments,
        ),
        candidate_ids=tuple(row.candidate_id for row in candidates),
        segment_profiles=segment_profiles,
        ug_handover=ug_handover,
        ur_handover=ur_handover,
        actual_directed_segment_count=len(
            segments if signature_segments is None else signature_segments
        ),
        preprocessing_time_s=perf_counter() - started,
        coarse_state_count=len(unique_points),
        ur_transition_refinement_calls=refinement_calls,
    )


def save_cache(
    cache: CommunicationGeometryCache,
    path: Path = CACHE_FILE,
) -> int:
    paths.ensure_output_path(path)
    with gzip.open(path, "wb", compresslevel=5) as stream:
        pickle.dump(cache, stream, protocol=pickle.HIGHEST_PROTOCOL)
    paths.register_generated_cache(path)
    return path.stat().st_size


def load_cache(path: Path = CACHE_FILE) -> CommunicationGeometryCache:
    paths.assert_trusted_cache(path)
    with gzip.open(path, "rb") as stream:
        cache = pickle.load(stream)
    if not isinstance(cache, CommunicationGeometryCache):
        raise TypeError("通信几何缓存类型不正确")
    if cache.version != CACHE_VERSION:
        raise ValueError("通信几何缓存版本不匹配")
    return cache


def export_manifest(
    cache: CommunicationGeometryCache,
    cache_file_size_bytes: int,
    path: Path = MANIFEST_FILE,
) -> None:
    summary = pd.DataFrame(
        [
            {"Metric": "Cache_Version", "Value": cache.version},
            {"Metric": "Signature", "Value": cache.signature},
            {"Metric": "Cached_Directed_Segments", "Value": len(cache.segment_profiles)},
            {"Metric": "Universe_Directed_Segments", "Value": cache.actual_directed_segment_count},
            {"Metric": "Remaining_Directed_Segments", "Value": cache.actual_directed_segment_count - len(cache.segment_profiles)},
            {"Metric": "UG_Segment_Profiles", "Value": len(cache.segment_profiles)},
            {"Metric": "UR_Segment_Candidate_Profiles", "Value": len(cache.segment_profiles) * len(cache.candidate_ids)},
            {"Metric": "UG_Handover_Profiles", "Value": len(cache.ug_handover)},
            {"Metric": "UR_Handover_Profiles", "Value": len(cache.ur_handover) * len(cache.candidate_ids)},
            {"Metric": "Elite_Candidates", "Value": len(cache.candidate_ids)},
            {"Metric": "Coarse_Unique_States", "Value": cache.coarse_state_count},
            {"Metric": "Transition_Refinement_Calls", "Value": cache.ur_transition_refinement_calls},
            {"Metric": "Preprocessing_Time_s", "Value": cache.preprocessing_time_s},
            {"Metric": "Average_Pair_Time_s", "Value": cache.preprocessing_time_s / max(len(cache.segment_profiles) * len(cache.candidate_ids), 1)},
            {"Metric": "Cache_File_Size_Bytes", "Value": cache_file_size_bytes},
        ]
    )
    segment_rows = []
    for profile in cache.segment_profiles.values():
        geometry = profile.geometry
        segment_rows.append(
            {
                "Segment_ID": geometry.segment_id,
                "Origin": geometry.origin,
                "Destination": geometry.destination,
                "Horizontal_Distance_m": geometry.horizontal_distance_m,
                "H_Flight_m": geometry.flight_altitude_m,
                "H_Up_m": geometry.climb_height_m,
                "H_Down_m": geometry.descent_height_m,
                "UG_Transition_Count": sum(
                    len(row.transition_q) for row in profile.ug_by_phase.values()
                ),
                "UR_Transition_Count": sum(
                    len(row.transition_q)
                    for profiles in profile.ur_by_phase.values()
                    for row in profiles
                ),
            }
        )
    paths.ensure_output_path(path)
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        pd.DataFrame(segment_rows).to_excel(
            writer, sheet_name="Segment_Manifest", index=False
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Problem 3 Phase A.5 fixed-segment communication cache"
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="构建全部实际可行有向航段并保存正式持久化缓存",
    )
    parser.add_argument(
        "--benchmark-segments",
        type=int,
        default=10,
        help="非full模式下选取的最长航段数，默认10",
    )
    parser.add_argument(
        "--workers", type=int, default=relay.DEFAULT_WORKER_COUNT
    )
    arguments = parser.parse_args()
    if arguments.workers < 1:
        raise ValueError("workers必须至少为1")
    if arguments.benchmark_segments < 1:
        raise ValueError("benchmark-segments必须至少为1")

    # 延迟导入，避免 problem3_alns 导入本模块时形成循环依赖。
    import problem3_alns as alns

    context = alns.load_phase_a_context()
    table = pd.read_excel(paths.require_file(alns.ELITE_POOL_FILE), sheet_name="Elite_Candidates")
    candidates = tuple(
        alns._candidate_from_row(row) for row in table.itertuples(index=False)
    )
    all_segments = actual_possible_directed_segments(context.transport_data)
    if arguments.full:
        selected = all_segments
    else:
        selected = tuple(
            sorted(
                all_segments,
                key=lambda pair: float(
                    context.transport_data.distance.at[
                        core._to_segment_node(pair[0]),
                        core._to_segment_node(pair[1]),
                    ]
                ),
                reverse=True,
            )[: arguments.benchmark_segments]
        )
    result = build_geometry_cache(
        context,
        candidates,
        selected,
        worker_count=arguments.workers,
        progress=True,
    )
    pair_count = len(selected) * len(candidates)
    print(
        f"preprocessing={result.preprocessing_time_s:.6f}s; "
        f"segments={len(selected)}; pairs={pair_count}; "
        f"average_pair={result.preprocessing_time_s / max(pair_count, 1):.9f}s",
        flush=True,
    )
    if arguments.full:
        size = save_cache(result, CACHE_FILE)
        export_manifest(result, size, MANIFEST_FILE)
        print(f"cache={CACHE_FILE}; size={size} bytes", flush=True)
        print(f"manifest={MANIFEST_FILE}", flush=True)
    else:
        estimate = result.preprocessing_time_s * len(all_segments) / len(selected)
        print(
            f"estimated_full={estimate:.3f}s "
            f"({estimate / 60.0:.2f}min); no cache written",
            flush=True,
        )


if __name__ == "__main__":
    # Serialize stable historical class names even when this file is launched directly.
    import problem3_segment_communication_cache as canonical_cache

    canonical_cache.main()
