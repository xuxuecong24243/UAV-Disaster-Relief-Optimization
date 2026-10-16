"""问题三 Step 4：中继候选悬停点、通信覆盖矩阵与加权贪心选址。

本模块只研究静态空间通信覆盖，不构造中继架次，不调度 R01/R02 或能源组件。
候选中继点的地面海拔来自 DEM；RG、UR 均使用问题三通信模块的完整链路口径。
"""

from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
import argparse
import math
import os
import sys

import numpy as np
import pandas as pd
from pyproj import Transformer


# Preserve historical module names used by trusted communication caches.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
import project_paths as paths
paths.bootstrap()
MODULE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = paths.ROOT

import problem1_segment_preprocessing as p1_segment  # noqa: E402
import problem3_communication as communication  # noqa: E402
import problem3_core as core  # noqa: E402


STEP3_FILE = paths.input_file("problem3", "problem3_transport_communication_check.xlsx")
OUTPUT_FILE = paths.output_file("problem3", "problem3_relay_coverage.xlsx")

# 确定性分层候选参数。
RELAY_COARSE_GRID_SPACING_M = 300.0
RELAY_FINE_GRID_SPACING_M = 100.0
RELAY_FINE_SEARCH_RADIUS_M = 300.0
RELAY_HEIGHT_LEVELS_M: Tuple[float, ...] = (50.0, 100.0, 150.0, 200.0, 250.0, 300.0)
RELAY_MAX_HOVER_HEIGHT_M = 300.0

# 镇龙乡位于 WGS84 / UTM zone 49N；投影只用于生成等间距网格和安全空间索引。
# 最终三维距离仍由 problem3_communication.py 的 WGS84 测地距离计算。
LOCAL_PROJECTED_CRS = "EPSG:32649"
WGS84_CRS = "EPSG:4326"
PROJECT_TO_LOCAL = Transformer.from_crs(WGS84_CRS, LOCAL_PROJECTED_CRS, always_xy=True)
PROJECT_TO_WGS84 = Transformer.from_crs(LOCAL_PROJECTED_CRS, WGS84_CRS, always_xy=True)

DEMAND_TOTAL_EXPECTED_S = 13445.3828125
NUMERIC_TOLERANCE = 1e-9
COORDINATE_KEY_DECIMALS = 10
STATE_KEY_DECIMALS = (10, 10, 6)
DEFAULT_WORKER_COUNT = min(8, max(1, (os.cpu_count() or 2) - 1))


@dataclass(frozen=True)
class HorizontalSite:
    site_key: str
    longitude_deg: float
    latitude_deg: float
    ground_elevation_m: float
    projected_x_m: float
    projected_y_m: float
    sources: Tuple[str, ...]


@dataclass
class RelayCandidate:
    candidate_id: str
    level: str
    site_key: str
    longitude_deg: float
    latitude_deg: float
    ground_elevation_m: float
    hover_height_m: float
    absolute_altitude_m: float
    sources: Tuple[str, ...]
    segment_maximum_ground_elevation_m: float = math.nan
    terrain_clearance_altitude_m: float = math.nan
    actual_flight_altitude_m: float = math.nan
    outbound_climb_height_m: float = math.nan
    outbound_descent_height_m: float = math.nan
    return_climb_height_m: float = math.nan
    return_descent_height_m: float = math.nan
    rg_distance_km: float = math.nan
    rg_obstruction: Optional[int] = None
    rg_fspl_db: float = math.nan
    rg_path_loss_db: float = math.nan
    rg_maximum_path_loss_db: float = math.nan
    rg_available: bool = False
    covered_state_indices: np.ndarray = field(
        default_factory=lambda: np.empty(0, dtype=np.int32), repr=False
    )
    covered_demand_count: int = 0
    covered_duration_s: float = 0.0
    covered_outage_count: int = 0
    covered_uav_count: int = 0


@dataclass(frozen=True)
class SpatialDemandStates:
    longitude_deg: np.ndarray
    latitude_deg: np.ndarray
    altitude_m: np.ndarray
    projected_x_m: np.ndarray
    projected_y_m: np.ndarray
    duration_weight_s: np.ndarray
    demand_count: np.ndarray
    member_demand_indices: Tuple[np.ndarray, ...]
    outage_ids: Tuple[frozenset[str], ...]
    uav_ids: Tuple[frozenset[str], ...]

    @property
    def count(self) -> int:
        return len(self.longitude_deg)


@dataclass(frozen=True)
class GreedyRound:
    iteration: int
    candidate_id: str
    newly_covered_state_indices: np.ndarray
    newly_covered_duration_s: float
    candidate_total_covered_duration_s: float
    cumulative_covered_duration_s: float
    remaining_uncovered_duration_s: float


@dataclass
class Step4Result:
    demands: Tuple[core.CommunicationDemand, ...]
    states: SpatialDemandStates
    candidates: Tuple[RelayCandidate, ...]
    level1_candidate_count: int
    level1_rg_available_count: int
    level1_greedy: Tuple[GreedyRound, ...]
    final_greedy: Tuple[GreedyRound, ...]
    uncovered_demand_indices: np.ndarray
    runtime_s: float


def load_step3_demands(
    path: Path = STEP3_FILE,
) -> Tuple[Tuple[core.CommunicationDemand, ...], pd.DataFrame]:
    """从 Step 3 检查工作簿恢复正式 CommunicationDemand。"""
    demand_table = pd.read_excel(paths.require_file(path), sheet_name="Outage_Demands")
    outage_table = pd.read_excel(paths.require_file(path), sheet_name="Outage_Intervals")
    demands = tuple(
        core.CommunicationDemand(
            demand_id=str(row.Demand_ID),
            outage_id=str(row.Outage_ID),
            transport_uav_id=str(row.Transport_UAV_ID),
            sortie_id=str(row.Sortie_ID),
            sample_time_s=float(row.Sample_Time_s),
            interval_start_s=float(row.Interval_Start_s),
            interval_end_s=float(row.Interval_End_s),
            duration_weight_s=float(row.Duration_Weight_s),
            phase=str(row.Phase),
            point=communication.Point3D(
                float(row.Longitude),
                float(row.Latitude),
                float(row.Absolute_Altitude_m),
            ),
        )
        for row in demand_table.itertuples(index=False)
    )
    total_weight = sum(row.duration_weight_s for row in demands)
    if len(demands) != 13453:
        raise ValueError(f"Step 3 demand 数量异常: {len(demands)}")
    if not math.isclose(total_weight, DEMAND_TOTAL_EXPECTED_S, abs_tol=1e-6):
        raise ValueError(f"Step 3 demand 权重异常: {total_weight:.12f} s")
    return demands, outage_table


def aggregate_spatial_demand_states(
    demands: Sequence[core.CommunicationDemand],
) -> SpatialDemandStates:
    """聚合通信位置完全相同的需求，覆盖结果最终仍映射回每条原始 demand。"""
    groups: Dict[Tuple[float, float, float], List[int]] = {}
    for index, demand in enumerate(demands):
        key = (
            round(demand.point.longitude_deg, STATE_KEY_DECIMALS[0]),
            round(demand.point.latitude_deg, STATE_KEY_DECIMALS[1]),
            round(demand.point.altitude_m, STATE_KEY_DECIMALS[2]),
        )
        groups.setdefault(key, []).append(index)

    keys = tuple(groups)
    longitude = np.asarray([key[0] for key in keys], dtype=np.float64)
    latitude = np.asarray([key[1] for key in keys], dtype=np.float64)
    altitude = np.asarray([key[2] for key in keys], dtype=np.float64)
    projected_x, projected_y = PROJECT_TO_LOCAL.transform(longitude, latitude)
    members = tuple(np.asarray(groups[key], dtype=np.int32) for key in keys)
    weights = np.asarray(
        [sum(demands[i].duration_weight_s for i in indexes) for indexes in members],
        dtype=np.float64,
    )
    counts = np.asarray([len(indexes) for indexes in members], dtype=np.int32)
    outage_ids = tuple(
        frozenset(demands[i].outage_id for i in indexes) for indexes in members
    )
    uav_ids = tuple(
        frozenset(demands[i].transport_uav_id for i in indexes) for indexes in members
    )
    if not math.isclose(float(weights.sum()), DEMAND_TOTAL_EXPECTED_S, abs_tol=1e-6):
        raise AssertionError("空间状态聚合改变了需求总权重")
    return SpatialDemandStates(
        longitude_deg=longitude,
        latitude_deg=latitude,
        altitude_m=altitude,
        projected_x_m=np.asarray(projected_x, dtype=np.float64),
        projected_y_m=np.asarray(projected_y, dtype=np.float64),
        duration_weight_s=weights,
        demand_count=counts,
        member_demand_indices=members,
        outage_ids=outage_ids,
        uav_ids=uav_ids,
    )


def maximum_distance_for_path_loss_m(
    maximum_path_loss_db: float,
    frequency_mhz: float,
    extra_loss_db: float = 0.0,
) -> float:
    """由 FSPL 门限反解三维最大距离，仅用于安全排除不可能链路。"""
    exponent = (
        maximum_path_loss_db
        - extra_loss_db
        - 32.45
        - 20.0 * math.log10(frequency_mhz)
    ) / 20.0
    return 1000.0 * 10.0**exponent


def _valid_dem_site(
    longitude_deg: float,
    latitude_deg: float,
    dem: communication.DemData,
) -> Optional[Tuple[float, int, int]]:
    col_float, row_float = (~dem.transform) * (longitude_deg, latitude_deg)
    row = int(math.floor(row_float))
    col = int(math.floor(col_float))
    height, width = dem.elevation_m.shape
    if not (0 <= row < height and 0 <= col < width):
        return None
    ground = float(dem.elevation_m[row, col])
    if not math.isfinite(ground):
        return None
    return ground, row, col


def _dem_cell_center(
    longitude_deg: float,
    latitude_deg: float,
    dem: communication.DemData,
) -> Optional[Tuple[float, float, float]]:
    valid = _valid_dem_site(longitude_deg, latitude_deg, dem)
    if valid is None:
        return None
    ground, row, col = valid
    lon, lat = dem.transform * (col + 0.5, row + 0.5)
    return float(lon), float(lat), ground


def _site_coordinate_key(longitude_deg: float, latitude_deg: float) -> Tuple[float, float]:
    return (
        round(float(longitude_deg), COORDINATE_KEY_DECIMALS),
        round(float(latitude_deg), COORDINATE_KEY_DECIMALS),
    )


def _geodesic_interpolate(
    start: communication.Point3D,
    end: communication.Point3D,
    fraction: float,
) -> Tuple[float, float]:
    azimuth, _, distance_m = communication.GEOD.inv(
        start.longitude_deg,
        start.latitude_deg,
        end.longitude_deg,
        end.latitude_deg,
    )
    lon, lat, _ = communication.GEOD.fwd(
        start.longitude_deg,
        start.latitude_deg,
        azimuth,
        float(distance_m) * fraction,
    )
    return float(lon), float(lat)


def generate_level1_horizontal_sites(
    demands: Sequence[core.CommunicationDemand],
    outage_table: pd.DataFrame,
    gateway: communication.GatewayEndpoint,
    dem: communication.DemData,
) -> Tuple[HorizontalSite, ...]:
    """生成全 DEM 300 m 粗网格，并加入三类确定性增强位置。"""
    raw_sites: Dict[Tuple[float, float], Dict[str, object]] = {}

    def add_site(lon: float, lat: float, source: str, snap_to_dem: bool) -> None:
        if snap_to_dem:
            centered = _dem_cell_center(lon, lat, dem)
            if centered is None:
                return
            lon, lat, ground = centered
        else:
            valid = _valid_dem_site(lon, lat, dem)
            if valid is None:
                return
            ground = valid[0]
        key = _site_coordinate_key(lon, lat)
        if key not in raw_sites:
            x_m, y_m = PROJECT_TO_LOCAL.transform(lon, lat)
            raw_sites[key] = {
                "longitude": float(lon),
                "latitude": float(lat),
                "ground": float(ground),
                "x": float(x_m),
                "y": float(y_m),
                "sources": set(),
            }
        raw_sites[key]["sources"].add(source)  # type: ignore[union-attr]

    # 全 DEM 的等间距投影网格；每个反投影点仍须落入有效 DEM 像元。
    corners_lon = np.asarray(
        [dem.bounds.left, dem.bounds.left, dem.bounds.right, dem.bounds.right]
    )
    corners_lat = np.asarray(
        [dem.bounds.bottom, dem.bounds.top, dem.bounds.bottom, dem.bounds.top]
    )
    corners_x, corners_y = PROJECT_TO_LOCAL.transform(corners_lon, corners_lat)
    x_values = np.arange(
        math.ceil(min(corners_x) / RELAY_COARSE_GRID_SPACING_M)
        * RELAY_COARSE_GRID_SPACING_M,
        max(corners_x) + NUMERIC_TOLERANCE,
        RELAY_COARSE_GRID_SPACING_M,
    )
    y_values = np.arange(
        math.ceil(min(corners_y) / RELAY_COARSE_GRID_SPACING_M)
        * RELAY_COARSE_GRID_SPACING_M,
        max(corners_y) + NUMERIC_TOLERANCE,
        RELAY_COARSE_GRID_SPACING_M,
    )
    grid_x, grid_y = np.meshgrid(x_values, y_values)
    grid_lon, grid_lat = PROJECT_TO_WGS84.transform(grid_x.ravel(), grid_y.ravel())
    for lon, lat in zip(grid_lon, grid_lat):
        add_site(float(lon), float(lat), "COARSE_GRID", snap_to_dem=False)

    # 每个 outage demand 的水平位置强制加入其所在 DEM 像元中心；重复像元自动合并。
    for demand in demands:
        add_site(
            demand.point.longitude_deg,
            demand.point.latitude_deg,
            "DEMAND_NEARBY_DEM_CELL",
            snap_to_dem=True,
        )

    demands_by_outage: Dict[str, List[core.CommunicationDemand]] = {}
    for demand in demands:
        demands_by_outage.setdefault(demand.outage_id, []).append(demand)

    # 每个 outage 取起点、四分位点、中点、四分之三点、终点作为代表位置。
    representative_points: List[communication.Point3D] = []
    for outage_id in outage_table["Outage_ID"].astype(str):
        rows = demands_by_outage[outage_id]
        for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
            index = int(round(fraction * (len(rows) - 1)))
            point = rows[index].point
            representative_points.append(point)
            add_site(
                point.longitude_deg,
                point.latitude_deg,
                "OUTAGE_REPRESENTATIVE",
                snap_to_dem=True,
            )

    # 在 G01 与 outage 代表位置间加入 1/4、1/2、3/4 三个测地线中间位置。
    for point in representative_points:
        for fraction in (0.25, 0.5, 0.75):
            lon, lat = _geodesic_interpolate(gateway.point, point, fraction)
            add_site(lon, lat, "G01_OUTAGE_INTERMEDIATE", snap_to_dem=True)

    sites: List[HorizontalSite] = []
    for index, (_, values) in enumerate(sorted(raw_sites.items()), start=1):
        sites.append(
            HorizontalSite(
                site_key=f"L1-S{index:06d}",
                longitude_deg=float(values["longitude"]),
                latitude_deg=float(values["latitude"]),
                ground_elevation_m=float(values["ground"]),
                projected_x_m=float(values["x"]),
                projected_y_m=float(values["y"]),
                sources=tuple(sorted(values["sources"])),
            )
        )
    return tuple(sites)


def expand_sites_to_candidates(
    sites: Sequence[HorizontalSite],
    level: str,
    start_index: int = 1,
) -> Tuple[RelayCandidate, ...]:
    candidates: List[RelayCandidate] = []
    index = start_index
    for site in sites:
        for hover_height in RELAY_HEIGHT_LEVELS_M:
            if not (0.0 < hover_height <= RELAY_MAX_HOVER_HEIGHT_M):
                raise AssertionError("中继悬停离地高度超出题目范围")
            candidates.append(
                RelayCandidate(
                    candidate_id=f"{level}-P{index:07d}",
                    level=level,
                    site_key=site.site_key,
                    longitude_deg=site.longitude_deg,
                    latitude_deg=site.latitude_deg,
                    ground_elevation_m=site.ground_elevation_m,
                    hover_height_m=hover_height,
                    absolute_altitude_m=site.ground_elevation_m + hover_height,
                    sources=site.sources,
                )
            )
            index += 1
    return tuple(candidates)


def _line_obstruction_for_first_altitudes(
    longitude_deg: float,
    latitude_deg: float,
    first_altitudes_m: np.ndarray,
    second: communication.Point3D,
    dem: communication.DemData,
) -> np.ndarray:
    """对同一水平视线的多个首端高度复用 DEM 穿越区间。"""
    matrix = _line_obstruction_matrix(
        longitude_deg,
        latitude_deg,
        first_altitudes_m,
        second.longitude_deg,
        second.latitude_deg,
        np.asarray([second.altitude_m], dtype=np.float64),
        dem,
    )
    return matrix[:, 0]


def validate_fixed_node_flight_altitude_extension() -> None:
    """断言统一实际飞行海拔公式不改变问题一/问题二固定节点结果。"""
    workbook = core.SEGMENT_FILE
    maximum_ground = pd.read_excel(
        paths.require_file(workbook), sheet_name="max_ground_elevation_matrix", index_col=0
    ).astype(float)
    original_cruise = pd.read_excel(
        paths.require_file(workbook), sheet_name="cruise_altitude_matrix", index_col=0
    ).astype(float)
    original_climb = pd.read_excel(
        paths.require_file(workbook), sheet_name="climb_height_matrix", index_col=0
    ).astype(float)
    original_descent = pd.read_excel(
        paths.require_file(workbook), sheet_name="descent_height_matrix", index_col=0
    ).astype(float)
    nodes = pd.read_excel(paths.require_file(workbook), sheet_name="node_elevation").set_index("node_id")
    operation_altitude = nodes["operation_altitude_m"].astype(float)

    maximum_altitude_error = 0.0
    maximum_climb_error = 0.0
    maximum_descent_error = 0.0
    for start in maximum_ground.index.astype(str):
        for end in maximum_ground.columns.astype(str):
            if start == end:
                continue
            terrain_clearance = float(maximum_ground.at[start, end]) + 50.0
            actual_flight = max(
                terrain_clearance,
                float(operation_altitude.at[start]),
                float(operation_altitude.at[end]),
            )
            climb = actual_flight - float(operation_altitude.at[start])
            descent = actual_flight - float(operation_altitude.at[end])
            maximum_altitude_error = max(
                maximum_altitude_error,
                abs(actual_flight - float(original_cruise.at[start, end])),
            )
            maximum_climb_error = max(
                maximum_climb_error,
                abs(climb - float(original_climb.at[start, end])),
            )
            maximum_descent_error = max(
                maximum_descent_error,
                abs(descent - float(original_descent.at[start, end])),
            )
            if climb < -NUMERIC_TOLERANCE or descent < -NUMERIC_TOLERANCE:
                raise AssertionError(f"固定节点航段出现负高度差: {start}->{end}")

    if maximum_altitude_error > 1e-9:
        raise AssertionError("统一公式改变了问题一固定节点计划巡航海拔")
    if maximum_climb_error > 1e-9 or maximum_descent_error > 1e-9:
        raise AssertionError("统一公式改变了问题一固定节点爬升/下降高度")
    print(
        "Fixed-node flight-altitude extension: PASS, "
        f"altitude_error={maximum_altitude_error:.3g}, "
        f"climb_error={maximum_climb_error:.3g}, "
        f"descent_error={maximum_descent_error:.3g}",
        flush=True,
    )


def calculate_candidate_flight_altitudes(
    candidates: Sequence[RelayCandidate],
    gateway: communication.GatewayEndpoint,
    dem: communication.DemData,
) -> None:
    """计算中继往返航段的地形净空基准和方案B实际飞行海拔。

    H_flight=max(H_DEM,max+50, H_operation(O01), H_operation(P))。
    候选通信端点仍为真实悬停绝对海拔，不以实际飞行海拔替代。
    航段经过像元与最高地面高程计算直接复用问题一正式预处理函数。
    """
    groups: Dict[str, List[RelayCandidate]] = {}
    for candidate in candidates:
        groups.setdefault(candidate.site_key, []).append(candidate)

    for count, rows in enumerate(groups.values(), start=1):
        sample = rows[0]
        crossed_cells = p1_segment.get_crossed_dem_cells(
            gateway.point.longitude_deg,
            gateway.point.latitude_deg,
            sample.longitude_deg,
            sample.latitude_deg,
            dem.transform,
            dem.elevation_m.shape,
        )
        maximum_ground_m = p1_segment.calculate_segment_max_elevation(
            dem.elevation_m, crossed_cells
        )
        terrain_clearance_altitude_m = maximum_ground_m + 50.0
        o01_operation_altitude_m = gateway.ground_elevation_m
        for candidate in rows:
            actual_flight_altitude_m = max(
                terrain_clearance_altitude_m,
                o01_operation_altitude_m,
                candidate.absolute_altitude_m,
            )
            candidate.segment_maximum_ground_elevation_m = maximum_ground_m
            candidate.terrain_clearance_altitude_m = terrain_clearance_altitude_m
            candidate.actual_flight_altitude_m = actual_flight_altitude_m
            candidate.outbound_climb_height_m = (
                actual_flight_altitude_m - o01_operation_altitude_m
            )
            candidate.outbound_descent_height_m = (
                actual_flight_altitude_m - candidate.absolute_altitude_m
            )
            candidate.return_climb_height_m = (
                actual_flight_altitude_m - candidate.absolute_altitude_m
            )
            candidate.return_descent_height_m = (
                actual_flight_altitude_m - o01_operation_altitude_m
            )
            if min(
                candidate.outbound_climb_height_m,
                candidate.outbound_descent_height_m,
                candidate.return_climb_height_m,
                candidate.return_descent_height_m,
            ) < -NUMERIC_TOLERANCE:
                raise AssertionError("方案B实际飞行海拔产生负爬升或下降高度")
        if count % 5000 == 0:
            print(
                f"Actual flight altitude evaluated horizontal sites: "
                f"{count}/{len(groups)}",
                flush=True,
            )


def _line_obstruction_matrix(
    first_longitude_deg: float,
    first_latitude_deg: float,
    first_altitudes_m: np.ndarray,
    second_longitude_deg: float,
    second_latitude_deg: float,
    second_altitudes_m: np.ndarray,
    dem: communication.DemData,
) -> np.ndarray:
    """完整比较三维视线与所有穿越 DEM 像元，批量处理两端高度组合。

    将视线与栅格列/行边界的交点参数一次排序。相邻参数之间位于唯一像元，
    边界交点处再检查所有相触像元，因此与 communication.analyze_line_of_sight
    的 all-touched 判定口径一致，但避免对每个高度重复构造像元集合。
    """
    first_altitudes_m = np.asarray(first_altitudes_m, dtype=np.float64)
    second_altitudes_m = np.asarray(second_altitudes_m, dtype=np.float64)
    result_shape = (len(first_altitudes_m), len(second_altitudes_m))
    if (
        abs(first_longitude_deg - second_longitude_deg) <= communication.TOLERANCE
        and abs(first_latitude_deg - second_latitude_deg) <= communication.TOLERANCE
    ):
        return np.zeros(result_shape, dtype=bool)

    inverse = ~dem.transform
    col0, row0 = inverse * (first_longitude_deg, first_latitude_deg)
    col1, row1 = inverse * (second_longitude_deg, second_latitude_deg)
    delta_col = col1 - col0
    delta_row = row1 - row0

    breakpoints = [np.asarray([0.0, 1.0], dtype=np.float64)]
    if abs(delta_col) > communication.TOLERANCE:
        boundaries = np.arange(
            math.floor(min(col0, col1)) + 1,
            math.floor(max(col0, col1)) + 1,
            dtype=np.float64,
        )
        values = (boundaries - col0) / delta_col
        breakpoints.append(values[(values > 1e-9) & (values < 1.0 - 1e-9)])
    if abs(delta_row) > communication.TOLERANCE:
        boundaries = np.arange(
            math.floor(min(row0, row1)) + 1,
            math.floor(max(row0, row1)) + 1,
            dtype=np.float64,
        )
        values = (boundaries - row0) / delta_row
        breakpoints.append(values[(values > 1e-9) & (values < 1.0 - 1e-9)])
    ordered = np.unique(np.concatenate(breakpoints))

    lower = ordered[:-1]
    upper = ordered[1:]
    midpoint = (lower + upper) / 2.0
    interval_cols = np.floor(col0 + delta_col * midpoint).astype(np.int64)
    interval_rows = np.floor(row0 + delta_row * midpoint).astype(np.int64)
    height, width = dem.elevation_m.shape
    valid = (
        (interval_rows >= 0)
        & (interval_rows < height)
        & (interval_cols >= 0)
        & (interval_cols < width)
    )
    lower = np.maximum(lower[valid], 1e-9)
    upper = np.minimum(upper[valid], 1.0 - 1e-9)
    interval_rows = interval_rows[valid]
    interval_cols = interval_cols[valid]
    interval_ground = dem.elevation_m[interval_rows, interval_cols]
    if not bool(np.all(np.isfinite(interval_ground))):
        raise ValueError("视线经过无效 DEM 像元")

    first_grid = first_altitudes_m[:, None, None]
    second_grid = second_altitudes_m[None, :, None]
    altitude_lower = first_grid + (second_grid - first_grid) * lower[None, None, :]
    altitude_upper = first_grid + (second_grid - first_grid) * upper[None, None, :]
    minimum_altitude = np.minimum(altitude_lower, altitude_upper)
    obstructed = np.any(
        minimum_altitude - interval_ground[None, None, :] <= 1e-6,
        axis=2,
    )

    # 精确穿过栅格边/角时，把所有相触像元作为零长度区间批量检查。
    boundary_parameters = ordered[1:-1]
    if len(boundary_parameters):
        col_values = col0 + delta_col * boundary_parameters
        row_values = row0 + delta_row * boundary_parameters
        base_cols = np.floor(col_values).astype(np.int64)
        base_rows = np.floor(row_values).astype(np.int64)
        on_col_boundary = np.isclose(
            col_values, np.rint(col_values), rtol=0.0, atol=1e-10
        )
        on_row_boundary = np.isclose(
            row_values, np.rint(row_values), rtol=0.0, atol=1e-10
        )
        maximum_ground = np.full(len(boundary_parameters), -math.inf)
        for row_offset, col_offset, use_mask in (
            (0, 0, np.ones(len(boundary_parameters), dtype=bool)),
            (0, -1, on_col_boundary),
            (-1, 0, on_row_boundary),
            (-1, -1, on_col_boundary & on_row_boundary),
        ):
            rows = base_rows + row_offset
            cols = base_cols + col_offset
            valid_cells = (
                use_mask
                & (rows >= 0)
                & (rows < height)
                & (cols >= 0)
                & (cols < width)
            )
            grounds = np.full(len(boundary_parameters), -math.inf)
            grounds[valid_cells] = dem.elevation_m[
                rows[valid_cells], cols[valid_cells]
            ]
            maximum_ground = np.maximum(maximum_ground, grounds)
        if not bool(np.all(np.isfinite(maximum_ground))):
            raise ValueError("视线经过无效 DEM 像元")
        line_altitude = first_altitudes_m[:, None, None] + (
            second_altitudes_m[None, :, None]
            - first_altitudes_m[:, None, None]
        ) * boundary_parameters[None, None, :]
        obstructed |= np.any(
            line_altitude - maximum_ground[None, None, :] <= 1e-6,
            axis=2,
        )

    return obstructed


def evaluate_rg_candidates(
    candidates: Sequence[RelayCandidate],
    gateway: communication.GatewayEndpoint,
    parameters: communication.CommunicationParameters,
    dem: communication.DemData,
) -> None:
    """对全部合法悬停高度层成组计算静态 RG，再做距离安全排除。"""
    threshold = communication.standard_link_thresholds_db(parameters)["RG"]
    max_distance_m = maximum_distance_for_path_loss_m(
        threshold, parameters.frequency_mhz
    )
    groups: Dict[str, List[RelayCandidate]] = {}
    for candidate in candidates:
        if not math.isfinite(candidate.actual_flight_altitude_m):
            raise ValueError("RG 筛选前必须先计算方案B实际飞行海拔")
        groups.setdefault(candidate.site_key, []).append(candidate)

    for count, rows in enumerate(groups.values(), start=1):
        first = rows[0]
        horizontal_m = communication.horizontal_distance_m(
            communication.Point3D(first.longitude_deg, first.latitude_deg, 0.0),
            communication.Point3D(
                gateway.point.longitude_deg, gateway.point.latitude_deg, 0.0
            ),
        )
        altitudes = np.asarray([row.absolute_altitude_m for row in rows])
        distances_m = np.hypot(horizontal_m, altitudes - gateway.point.altitude_m)
        fspl = np.asarray(
            [communication.fspl_db(parameters.frequency_mhz, value / 1000.0) for value in distances_m]
        )
        possible = distances_m <= max_distance_m + NUMERIC_TOLERANCE
        obstruction = np.zeros(len(rows), dtype=bool)
        if bool(np.any(possible)):
            obstruction[possible] = _line_obstruction_for_first_altitudes(
                first.longitude_deg,
                first.latitude_deg,
                altitudes[possible],
                gateway.point,
                dem,
            )
        path_loss = fspl + parameters.obstruction_loss_db * obstruction.astype(float)
        available = possible & (path_loss <= threshold + communication.TOLERANCE)
        for i, candidate in enumerate(rows):
            candidate.rg_distance_km = float(distances_m[i] / 1000.0)
            candidate.rg_obstruction = int(obstruction[i]) if possible[i] else None
            candidate.rg_fspl_db = float(fspl[i])
            candidate.rg_path_loss_db = float(path_loss[i])
            candidate.rg_maximum_path_loss_db = float(threshold)
            candidate.rg_available = bool(available[i])
        if count % 2500 == 0:
            print(f"RG evaluated horizontal sites: {count}/{len(groups)}", flush=True)


# Process-worker globals for exact UR coverage evaluation.
_WORKER_DEM: Optional[communication.DemData] = None
_WORKER_PARAMETERS: Optional[communication.CommunicationParameters] = None
_WORKER_STATE_LONGITUDE: Optional[np.ndarray] = None
_WORKER_STATE_LATITUDE: Optional[np.ndarray] = None
_WORKER_STATE_ALTITUDE: Optional[np.ndarray] = None
_WORKER_STATE_X: Optional[np.ndarray] = None
_WORKER_STATE_Y: Optional[np.ndarray] = None
_WORKER_XY_LONGITUDE: Optional[np.ndarray] = None
_WORKER_XY_LATITUDE: Optional[np.ndarray] = None
_WORKER_XY_X: Optional[np.ndarray] = None
_WORKER_XY_Y: Optional[np.ndarray] = None
_WORKER_XY_STATE_INDICES: Optional[Tuple[np.ndarray, ...]] = None
_WORKER_UR_THRESHOLD: float = math.nan
_WORKER_UR_MAX_DISTANCE_M: float = math.nan


def _initialize_coverage_worker(
    longitude: np.ndarray,
    latitude: np.ndarray,
    altitude: np.ndarray,
    projected_x: np.ndarray,
    projected_y: np.ndarray,
) -> None:
    global _WORKER_DEM, _WORKER_PARAMETERS
    global _WORKER_STATE_LONGITUDE, _WORKER_STATE_LATITUDE, _WORKER_STATE_ALTITUDE
    global _WORKER_STATE_X, _WORKER_STATE_Y, _WORKER_UR_THRESHOLD
    global _WORKER_XY_LONGITUDE, _WORKER_XY_LATITUDE, _WORKER_XY_X, _WORKER_XY_Y
    global _WORKER_XY_STATE_INDICES
    global _WORKER_UR_MAX_DISTANCE_M
    _WORKER_DEM = communication.load_dem()
    _WORKER_PARAMETERS = communication.load_communication_parameters()
    _WORKER_STATE_LONGITUDE = longitude
    _WORKER_STATE_LATITUDE = latitude
    _WORKER_STATE_ALTITUDE = altitude
    _WORKER_STATE_X = projected_x
    _WORKER_STATE_Y = projected_y
    xy_groups: Dict[Tuple[float, float], List[int]] = {}
    for index, (lon, lat) in enumerate(zip(longitude, latitude)):
        xy_groups.setdefault((float(lon), float(lat)), []).append(index)
    xy_keys = tuple(xy_groups)
    _WORKER_XY_LONGITUDE = np.asarray([key[0] for key in xy_keys], dtype=np.float64)
    _WORKER_XY_LATITUDE = np.asarray([key[1] for key in xy_keys], dtype=np.float64)
    _WORKER_XY_X, _WORKER_XY_Y = PROJECT_TO_LOCAL.transform(
        _WORKER_XY_LONGITUDE, _WORKER_XY_LATITUDE
    )
    _WORKER_XY_X = np.asarray(_WORKER_XY_X, dtype=np.float64)
    _WORKER_XY_Y = np.asarray(_WORKER_XY_Y, dtype=np.float64)
    _WORKER_XY_STATE_INDICES = tuple(
        np.asarray(xy_groups[key], dtype=np.int32) for key in xy_keys
    )
    _WORKER_UR_THRESHOLD = communication.standard_link_thresholds_db(
        _WORKER_PARAMETERS
    )["UR"]
    _WORKER_UR_MAX_DISTANCE_M = maximum_distance_for_path_loss_m(
        _WORKER_UR_THRESHOLD, _WORKER_PARAMETERS.frequency_mhz
    )


def _coverage_worker(
    task: Tuple[
        float,
        float,
        float,
        float,
        Tuple[Tuple[int, float], ...],
    ]
) -> Tuple[Tuple[int, np.ndarray], ...]:
    """计算一个水平位置各 RG 可用高度的完整 UR 覆盖状态。"""
    lon, lat, x_m, y_m, candidate_heights = task
    assert _WORKER_DEM is not None and _WORKER_PARAMETERS is not None
    assert _WORKER_STATE_LONGITUDE is not None and _WORKER_STATE_LATITUDE is not None
    assert _WORKER_STATE_ALTITUDE is not None
    assert _WORKER_STATE_X is not None and _WORKER_STATE_Y is not None
    assert _WORKER_XY_LONGITUDE is not None and _WORKER_XY_LATITUDE is not None
    assert _WORKER_XY_X is not None and _WORKER_XY_Y is not None
    assert _WORKER_XY_STATE_INDICES is not None

    # 投影距离只作宽松预筛；1% 裕量保证不会误排除潜在链路。
    planar = np.hypot(_WORKER_XY_X - x_m, _WORKER_XY_Y - y_m)
    nearby_xy = np.flatnonzero(planar <= _WORKER_UR_MAX_DISTANCE_M * 1.01)
    if len(nearby_xy) == 0:
        return tuple(
            (candidate_index, np.empty(0, dtype=np.int32))
            for candidate_index, _ in candidate_heights
        )

    _, _, horizontal_m = communication.GEOD.inv(
        np.full(len(nearby_xy), lon),
        np.full(len(nearby_xy), lat),
        _WORKER_XY_LONGITUDE[nearby_xy],
        _WORKER_XY_LATITUDE[nearby_xy],
    )
    horizontal_m = np.abs(np.asarray(horizontal_m, dtype=np.float64))
    candidate_indices = np.asarray([row[0] for row in candidate_heights], dtype=np.int64)
    relay_altitudes = np.asarray([row[1] for row in candidate_heights], dtype=np.float64)
    covered: List[List[int]] = [[] for _ in candidate_heights]

    for local_index, xy_index in enumerate(nearby_xy):
        state_indices = _WORKER_XY_STATE_INDICES[int(xy_index)]
        state_altitudes = _WORKER_STATE_ALTITUDE[state_indices]
        distances_m = np.hypot(
            horizontal_m[local_index],
            relay_altitudes[:, None] - state_altitudes[None, :],
        )
        possible = distances_m <= _WORKER_UR_MAX_DISTANCE_M + NUMERIC_TOLERANCE
        if not bool(np.any(possible)):
            continue
        # 相同需求经纬度的不同飞行高度与多个中继高度共同复用水平视线路径。
        obstruction = _line_obstruction_matrix(
            lon,
            lat,
            relay_altitudes,
            float(_WORKER_XY_LONGITUDE[xy_index]),
            float(_WORKER_XY_LATITUDE[xy_index]),
            state_altitudes,
            _WORKER_DEM,
        )
        fspl = np.full(distances_m.shape, math.inf, dtype=np.float64)
        positive = possible & (distances_m > 0.0)
        fspl[positive] = (
            32.45
            + 20.0 * math.log10(_WORKER_PARAMETERS.frequency_mhz)
            + 20.0 * np.log10(distances_m[positive] / 1000.0)
        )
        fspl[possible & (distances_m == 0.0)] = -math.inf
        path_loss = fspl + _WORKER_PARAMETERS.obstruction_loss_db * obstruction
        available = possible & (
            path_loss <= _WORKER_UR_THRESHOLD + communication.TOLERANCE
        )
        for height_position in range(len(relay_altitudes)):
            covered_states = state_indices[available[height_position]]
            covered[height_position].extend(int(value) for value in covered_states)

    return tuple(
        (int(candidate_indices[position]), np.asarray(values, dtype=np.int32))
        for position, values in enumerate(covered)
    )


def precompute_ur_coverage(
    candidates: Sequence[RelayCandidate],
    states: SpatialDemandStates,
    worker_count: int = DEFAULT_WORKER_COUNT,
) -> None:
    """只对 RG 可用候选预计算 UR 覆盖；贪心阶段不再调用通信链路。"""
    grouped: Dict[str, List[Tuple[int, RelayCandidate]]] = {}
    for index, candidate in enumerate(candidates):
        if candidate.rg_available:
            grouped.setdefault(candidate.site_key, []).append((index, candidate))

    tasks = []
    for rows in grouped.values():
        sample = rows[0][1]
        x_m, y_m = PROJECT_TO_LOCAL.transform(
            sample.longitude_deg, sample.latitude_deg
        )
        tasks.append(
            (
                sample.longitude_deg,
                sample.latitude_deg,
                float(x_m),
                float(y_m),
                tuple((index, row.absolute_altitude_m) for index, row in rows),
            )
        )

    initializer_args = (
        states.longitude_deg,
        states.latitude_deg,
        states.altitude_m,
        states.projected_x_m,
        states.projected_y_m,
    )
    if worker_count <= 1:
        _initialize_coverage_worker(*initializer_args)
        iterator: Iterable[Tuple[Tuple[int, np.ndarray], ...]] = map(
            _coverage_worker, tasks
        )
        executor = None
    else:
        executor = ProcessPoolExecutor(
            max_workers=worker_count,
            initializer=_initialize_coverage_worker,
            initargs=initializer_args,
        )
        iterator = executor.map(_coverage_worker, tasks, chunksize=2)

    try:
        for completed, result in enumerate(iterator, start=1):
            for candidate_index, covered_states in result:
                candidate = candidates[candidate_index]
                candidate.covered_state_indices = covered_states
                if len(covered_states) == 0:
                    continue
                candidate.covered_duration_s = float(
                    states.duration_weight_s[covered_states].sum()
                )
                candidate.covered_demand_count = int(
                    states.demand_count[covered_states].sum()
                )
                candidate.covered_outage_count = len(
                    set().union(*(states.outage_ids[i] for i in covered_states))
                )
                candidate.covered_uav_count = len(
                    set().union(*(states.uav_ids[i] for i in covered_states))
                )
            if completed % 250 == 0:
                print(
                    f"UR coverage evaluated horizontal sites: {completed}/{len(tasks)}",
                    flush=True,
                )
    finally:
        if executor is not None:
            executor.shutdown(wait=True)


def weighted_greedy_cover(
    candidates: Sequence[RelayCandidate],
    states: SpatialDemandStates,
) -> Tuple[Tuple[GreedyRound, ...], np.ndarray]:
    """在预计算覆盖集合上执行确定性加权贪心，不重复计算链路。"""
    uncovered = np.ones(states.count, dtype=bool)
    rounds: List[GreedyRound] = []
    cumulative = 0.0
    while bool(np.any(uncovered)):
        best_candidate: Optional[RelayCandidate] = None
        best_gain = 0.0
        best_new_states = np.empty(0, dtype=np.int32)
        for candidate in candidates:
            indexes = candidate.covered_state_indices
            if len(indexes) == 0:
                continue
            new_states = indexes[uncovered[indexes]]
            if len(new_states) == 0:
                continue
            gain = float(states.duration_weight_s[new_states].sum())
            better = gain > best_gain + NUMERIC_TOLERANCE
            tied = math.isclose(gain, best_gain, abs_tol=NUMERIC_TOLERANCE)
            if tied and best_candidate is not None:
                better = (
                    candidate.hover_height_m,
                    candidate.candidate_id,
                ) < (
                    best_candidate.hover_height_m,
                    best_candidate.candidate_id,
                )
            if better or best_candidate is None:
                best_candidate = candidate
                best_gain = gain
                best_new_states = new_states
        if best_candidate is None or best_gain <= NUMERIC_TOLERANCE:
            break
        uncovered[best_new_states] = False
        cumulative += best_gain
        remaining = float(states.duration_weight_s[uncovered].sum())
        rounds.append(
            GreedyRound(
                iteration=len(rounds) + 1,
                candidate_id=best_candidate.candidate_id,
                newly_covered_state_indices=best_new_states,
                newly_covered_duration_s=best_gain,
                candidate_total_covered_duration_s=best_candidate.covered_duration_s,
                cumulative_covered_duration_s=cumulative,
                remaining_uncovered_duration_s=remaining,
            )
        )
        print(
            f"Greedy {len(rounds)}: {best_candidate.candidate_id}, "
            f"new={best_gain:.6f}s, remaining={remaining:.6f}s",
            flush=True,
        )
    return tuple(rounds), np.flatnonzero(uncovered).astype(np.int32)


def generate_fine_sites(
    selected_candidates: Sequence[RelayCandidate],
    existing_sites: Sequence[HorizontalSite],
    dem: communication.DemData,
) -> Tuple[HorizontalSite, ...]:
    """在粗网格入选点 ±300 m 邻域内生成确定性 100 m 局部网格。"""
    existing_keys = {
        _site_coordinate_key(row.longitude_deg, row.latitude_deg) for row in existing_sites
    }
    raw: Dict[Tuple[float, float], HorizontalSite] = {}
    offsets = np.arange(
        -RELAY_FINE_SEARCH_RADIUS_M,
        RELAY_FINE_SEARCH_RADIUS_M + NUMERIC_TOLERANCE,
        RELAY_FINE_GRID_SPACING_M,
    )
    for selected in selected_candidates:
        center_x, center_y = PROJECT_TO_LOCAL.transform(
            selected.longitude_deg, selected.latitude_deg
        )
        for dx in offsets:
            for dy in offsets:
                lon, lat = PROJECT_TO_WGS84.transform(center_x + dx, center_y + dy)
                valid = _valid_dem_site(float(lon), float(lat), dem)
                if valid is None:
                    continue
                key = _site_coordinate_key(float(lon), float(lat))
                if key in existing_keys or key in raw:
                    continue
                x_m, y_m = PROJECT_TO_LOCAL.transform(float(lon), float(lat))
                raw[key] = HorizontalSite(
                    site_key="",
                    longitude_deg=float(lon),
                    latitude_deg=float(lat),
                    ground_elevation_m=float(valid[0]),
                    projected_x_m=float(x_m),
                    projected_y_m=float(y_m),
                    sources=(f"FINE_AROUND_{selected.candidate_id}",),
                )
    sites = []
    for index, (_, site) in enumerate(sorted(raw.items()), start=1):
        sites.append(
            HorizontalSite(
                site_key=f"L2-S{index:06d}",
                longitude_deg=site.longitude_deg,
                latitude_deg=site.latitude_deg,
                ground_elevation_m=site.ground_elevation_m,
                projected_x_m=site.projected_x_m,
                projected_y_m=site.projected_y_m,
                sources=site.sources,
            )
        )
    return tuple(sites)


def _selected_candidates(
    rounds: Sequence[GreedyRound],
    candidate_map: Mapping[str, RelayCandidate],
) -> Tuple[RelayCandidate, ...]:
    return tuple(candidate_map[row.candidate_id] for row in rounds)


def _expand_state_indices_to_demands(
    state_indices: Sequence[int], states: SpatialDemandStates
) -> np.ndarray:
    if len(state_indices) == 0:
        return np.empty(0, dtype=np.int32)
    return np.concatenate(
        [states.member_demand_indices[int(index)] for index in state_indices]
    ).astype(np.int32)


def _candidate_points_table(candidates: Sequence[RelayCandidate]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Candidate_ID": row.candidate_id,
                "Level": row.level,
                "Site_Key": row.site_key,
                "Longitude": row.longitude_deg,
                "Latitude": row.latitude_deg,
                "Ground_Elevation_m": row.ground_elevation_m,
                "Hover_Height_m": row.hover_height_m,
                "Absolute_Altitude_m": row.absolute_altitude_m,
                "Segment_Maximum_Ground_Elevation_m": row.segment_maximum_ground_elevation_m,
                "Terrain_Clearance_Altitude_m": row.terrain_clearance_altitude_m,
                "Actual_Flight_Altitude_m": row.actual_flight_altitude_m,
                "Outbound_Climb_Height_m": row.outbound_climb_height_m,
                "Outbound_Descent_Height_m": row.outbound_descent_height_m,
                "Return_Climb_Height_m": row.return_climb_height_m,
                "Return_Descent_Height_m": row.return_descent_height_m,
                "Candidate_Sources": ";".join(row.sources),
                "RG_Distance_km": row.rg_distance_km,
                "RG_Obstruction": row.rg_obstruction,
                "RG_FSPL_dB": row.rg_fspl_db,
                "RG_Path_Loss_dB": row.rg_path_loss_db,
                "RG_Lmax_dB": row.rg_maximum_path_loss_db,
                "RG_Available": int(row.rg_available),
            }
            for row in candidates
        ]
    )


def _coverage_summary_table(candidates: Sequence[RelayCandidate]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Candidate_ID": row.candidate_id,
                "Level": row.level,
                "RG_Available": int(row.rg_available),
                "Covered_Demand_Count": row.covered_demand_count,
                "Covered_Duration_s": row.covered_duration_s,
                "Covered_Percent": 100.0 * row.covered_duration_s / DEMAND_TOTAL_EXPECTED_S,
                "Covered_Outage_Count": row.covered_outage_count,
                "Covered_UAV_Count": row.covered_uav_count,
            }
            for row in candidates
        ]
    )


def _greedy_table(
    level1_rounds: Sequence[GreedyRound],
    final_rounds: Sequence[GreedyRound],
    candidate_map: Mapping[str, RelayCandidate],
    states: SpatialDemandStates,
) -> pd.DataFrame:
    rows = []
    for solution_name, rounds in (
        ("LEVEL1_COARSE", level1_rounds),
        ("FINAL_WITH_LOCAL_REFINEMENT", final_rounds),
    ):
        for greedy_round in rounds:
            candidate = candidate_map[greedy_round.candidate_id]
            new_states = greedy_round.newly_covered_state_indices
            outage_ids = sorted(
                set().union(*(states.outage_ids[i] for i in new_states))
            )
            uav_ids = sorted(set().union(*(states.uav_ids[i] for i in new_states)))
            rows.append(
                {
                    "Solution": solution_name,
                    "Greedy_Iteration": greedy_round.iteration,
                    "Candidate_ID": candidate.candidate_id,
                    "Level": candidate.level,
                    "Longitude": candidate.longitude_deg,
                    "Latitude": candidate.latitude_deg,
                    "Ground_Elevation_m": candidate.ground_elevation_m,
                    "Hover_Height_m": candidate.hover_height_m,
                    "Absolute_Altitude_m": candidate.absolute_altitude_m,
                    "Segment_Maximum_Ground_Elevation_m": candidate.segment_maximum_ground_elevation_m,
                    "Terrain_Clearance_Altitude_m": candidate.terrain_clearance_altitude_m,
                    "Actual_Flight_Altitude_m": candidate.actual_flight_altitude_m,
                    "Outbound_Climb_Height_m": candidate.outbound_climb_height_m,
                    "Outbound_Descent_Height_m": candidate.outbound_descent_height_m,
                    "Return_Climb_Height_m": candidate.return_climb_height_m,
                    "Return_Descent_Height_m": candidate.return_descent_height_m,
                    "RG_Available": int(candidate.rg_available),
                    "Candidate_Total_Covered_Duration_s": candidate.covered_duration_s,
                    "Newly_Covered_Duration_s": greedy_round.newly_covered_duration_s,
                    "Cumulative_Covered_Duration_s": greedy_round.cumulative_covered_duration_s,
                    "Remaining_Uncovered_Duration_s": greedy_round.remaining_uncovered_duration_s,
                    "Newly_Covered_Outage_IDs": ";".join(outage_ids),
                    "Newly_Covered_UAV_IDs": ";".join(uav_ids),
                    "Candidate_Covered_UAV_Count": candidate.covered_uav_count,
                }
            )
    return pd.DataFrame(rows)


def _demand_coverage_table(
    demands: Sequence[core.CommunicationDemand],
    states: SpatialDemandStates,
    final_rounds: Sequence[GreedyRound],
    candidate_map: Mapping[str, RelayCandidate],
) -> Tuple[pd.DataFrame, np.ndarray]:
    selected = _selected_candidates(final_rounds, candidate_map)
    state_to_selected: List[List[str]] = [[] for _ in range(states.count)]
    for candidate in selected:
        for state_index in candidate.covered_state_indices:
            state_to_selected[int(state_index)].append(candidate.candidate_id)
    demand_to_selected: List[List[str]] = [[] for _ in demands]
    for state_index, demand_indices in enumerate(states.member_demand_indices):
        for demand_index in demand_indices:
            demand_to_selected[int(demand_index)] = state_to_selected[state_index]
    rows = []
    uncovered = []
    for index, demand in enumerate(demands):
        covering = demand_to_selected[index]
        if not covering:
            uncovered.append(index)
        rows.append(
            {
                "Demand_ID": demand.demand_id,
                "Outage_ID": demand.outage_id,
                "Transport_UAV_ID": demand.transport_uav_id,
                "Sortie_ID": demand.sortie_id,
                "Sample_Time_s": demand.sample_time_s,
                "Duration_Weight_s": demand.duration_weight_s,
                "Phase": demand.phase,
                "Longitude": demand.point.longitude_deg,
                "Latitude": demand.point.latitude_deg,
                "Absolute_Altitude_m": demand.point.altitude_m,
                "Covered": int(bool(covering)),
                "Selected_Covering_Candidate_Count": len(covering),
                "Selected_Covering_Candidate_IDs": ";".join(covering),
            }
        )
    return pd.DataFrame(rows), np.asarray(uncovered, dtype=np.int32)


def _uncovered_table(
    demands: Sequence[core.CommunicationDemand], uncovered: Sequence[int]
) -> pd.DataFrame:
    columns = [
        "Demand_ID",
        "Outage_ID",
        "Transport_UAV_ID",
        "Sortie_ID",
        "Sample_Time_s",
        "Duration_Weight_s",
        "Phase",
        "Longitude",
        "Latitude",
        "Absolute_Altitude_m",
    ]
    return pd.DataFrame(
        [
            {
                "Demand_ID": demands[i].demand_id,
                "Outage_ID": demands[i].outage_id,
                "Transport_UAV_ID": demands[i].transport_uav_id,
                "Sortie_ID": demands[i].sortie_id,
                "Sample_Time_s": demands[i].sample_time_s,
                "Duration_Weight_s": demands[i].duration_weight_s,
                "Phase": demands[i].phase,
                "Longitude": demands[i].point.longitude_deg,
                "Latitude": demands[i].point.latitude_deg,
                "Absolute_Altitude_m": demands[i].point.altitude_m,
            }
            for i in uncovered
        ],
        columns=columns,
    )


def export_step4(result: Step4Result, output_file: Path = OUTPUT_FILE) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    candidate_map = {row.candidate_id: row for row in result.candidates}
    demand_coverage, uncovered = _demand_coverage_table(
        result.demands, result.states, result.final_greedy, candidate_map
    )
    tables = {
        "Candidate_Points": _candidate_points_table(result.candidates),
        "Candidate_Coverage_Summary": _coverage_summary_table(result.candidates),
        "Greedy_Selection": _greedy_table(
            result.level1_greedy,
            result.final_greedy,
            candidate_map,
            result.states,
        ),
        "Demand_Coverage": demand_coverage,
        "Uncovered_Demands": _uncovered_table(result.demands, uncovered),
    }
    paths.ensure_output_path(output_file)
    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        for sheet_name, table in tables.items():
            table.to_excel(writer, sheet_name=sheet_name, index=False)
        for worksheet in writer.book.worksheets:
            worksheet.freeze_panes = "A2"
            worksheet.auto_filter.ref = worksheet.dimensions
            for column in worksheet.columns:
                values = [
                    "" if cell.value is None else str(cell.value)
                    for cell in column[:1000]
                ]
                width = min(max(max(map(len, values), default=10) + 2, 12), 38)
                worksheet.column_dimensions[column[0].column_letter].width = width
                for cell in column[1:]:
                    if isinstance(cell.value, float):
                        cell.number_format = "0.000000000"


def analyze_saved_selected_concurrency(
    coverage_file: Path = paths.input_file("problem3", "problem3_relay_coverage.xlsx"),
    step3_file: Path = STEP3_FILE,
) -> pd.DataFrame:
    """核对最终入选位置是否在同一绝对时刻覆盖多架运输无人机。"""
    coverage = pd.read_excel(paths.require_file(coverage_file), sheet_name="Demand_Coverage")
    intervals = pd.read_excel(paths.require_file(step3_file), sheet_name="Outage_Demands")[[
        "Demand_ID",
        "Interval_Start_s",
        "Interval_End_s",
    ]]
    merged = coverage.merge(intervals, on="Demand_ID", validate="one_to_one")
    selected_ids = sorted(
        {
            candidate_id
            for text in merged["Selected_Covering_Candidate_IDs"].fillna("")
            for candidate_id in str(text).split(";")
            if candidate_id
        }
    )
    rows = []
    for candidate_id in selected_ids:
        mask = merged["Selected_Covering_Candidate_IDs"].fillna("").map(
            lambda text: candidate_id in str(text).split(";")
        )
        subset = merged.loc[mask]
        starts = subset[["Interval_Start_s", "Transport_UAV_ID"]].rename(
            columns={"Interval_Start_s": "Time_s"}
        )
        starts = starts.assign(Delta=1)
        ends = subset[["Interval_End_s", "Transport_UAV_ID"]].rename(
            columns={"Interval_End_s": "Time_s"}
        )
        ends = ends.assign(Delta=-1)
        event_table = pd.concat([starts, ends], ignore_index=True).pivot_table(
            index="Time_s",
            columns="Transport_UAV_ID",
            values="Delta",
            aggfunc="sum",
            fill_value=0,
        ).sort_index()
        active = event_table.cumsum()
        concurrent_count = (active > 0).sum(axis=1)
        maximum = int(concurrent_count.max())
        time_s = float(concurrent_count.idxmax())
        simultaneous_uavs = sorted(active.columns[active.loc[time_s] > 0].tolist())
        rows.append(
            {
                "Candidate_ID": candidate_id,
                "Maximum_Simultaneous_UAV_Count": maximum,
                "Representative_Time_s": time_s,
                "Simultaneous_UAV_IDs": ";".join(simultaneous_uavs),
                "Covered_Outage_IDs": ";".join(sorted(subset["Outage_ID"].unique())),
                "Covered_Duration_s": float(subset["Duration_Weight_s"].sum()),
            }
        )
    return pd.DataFrame(rows)


def _validate_grouped_los(
    candidates: Sequence[RelayCandidate],
    demands: Sequence[core.CommunicationDemand],
    states: SpatialDemandStates,
    parameters: communication.CommunicationParameters,
    gateway: communication.GatewayEndpoint,
    dem: communication.DemData,
) -> None:
    """抽取确定性样本，对照通信模块逐点函数验证成组 LOS/链路结果。"""
    rg_candidates = [row for row in candidates if row.rg_obstruction is not None]
    rg_samples = rg_candidates[:: max(1, len(rg_candidates) // 12)][:12]
    for candidate in rg_samples:
        relay = communication.Point3D(
            candidate.longitude_deg,
            candidate.latitude_deg,
            candidate.absolute_altitude_m,
        )
        exact = communication.evaluate_rg_link(relay, gateway.point, parameters, dem)
        if (
            int(exact.obstructed) != candidate.rg_obstruction
            or exact.available != candidate.rg_available
            or not math.isclose(
                exact.total_path_loss_db, candidate.rg_path_loss_db, abs_tol=1e-9
            )
        ):
            raise AssertionError(f"RG 成组计算与标准函数不一致: {candidate.candidate_id}")

    available = [row for row in candidates if row.rg_available and len(row.covered_state_indices)]
    for candidate in available[:6]:
        relay = communication.Point3D(
            candidate.longitude_deg,
            candidate.latitude_deg,
            candidate.absolute_altitude_m,
        )
        state_index = int(candidate.covered_state_indices[0])
        demand_index = int(states.member_demand_indices[state_index][0])
        exact = communication.evaluate_ur_link(
            demands[demand_index].point, relay, parameters, dem
        )
        if not exact.available:
            raise AssertionError(f"UR 覆盖样本无法由标准函数复核: {candidate.candidate_id}")
    print("Grouped LOS/link validation: PASS", flush=True)


def run_step4(
    export: bool = True,
    worker_count: int = DEFAULT_WORKER_COUNT,
) -> Step4Result:
    started = perf_counter()
    validate_fixed_node_flight_altitude_extension()
    demands, outage_table = load_step3_demands()
    states = aggregate_spatial_demand_states(demands)
    parameters = communication.load_communication_parameters()
    gateway = communication.load_g01_endpoint(parameters)
    dem = communication.load_dem()
    thresholds = communication.standard_link_thresholds_db(parameters)
    print(
        f"Loaded demands={len(demands)}, spatial_states={states.count}, "
        f"weight={states.duration_weight_s.sum():.6f}s",
        flush=True,
    )
    print(
        f"Lmax_UR={thresholds['UR']:.0f} dB, Lmax_RG={thresholds['RG']:.0f} dB, "
        f"workers={worker_count}",
        flush=True,
    )

    level1_sites = generate_level1_horizontal_sites(
        demands, outage_table, gateway, dem
    )
    level1_candidates = expand_sites_to_candidates(level1_sites, "L1")
    print(
        f"Level 1 horizontal sites={len(level1_sites)}, "
        f"candidate schemes={len(level1_candidates)}",
        flush=True,
    )
    calculate_candidate_flight_altitudes(level1_candidates, gateway, dem)
    evaluate_rg_candidates(level1_candidates, gateway, parameters, dem)
    level1_rg_available = sum(row.rg_available for row in level1_candidates)
    print(
        f"Level 1 RG available={level1_rg_available}/{len(level1_candidates)}",
        flush=True,
    )
    precompute_ur_coverage(level1_candidates, states, worker_count)
    level1_greedy, level1_uncovered_states = weighted_greedy_cover(
        level1_candidates, states
    )

    level1_map = {row.candidate_id: row for row in level1_candidates}
    selected_level1 = _selected_candidates(level1_greedy, level1_map)
    fine_sites = generate_fine_sites(selected_level1, level1_sites, dem)
    fine_candidates = expand_sites_to_candidates(fine_sites, "L2")
    print(
        f"Fine horizontal sites={len(fine_sites)}, candidate schemes={len(fine_candidates)}",
        flush=True,
    )
    if fine_candidates:
        calculate_candidate_flight_altitudes(fine_candidates, gateway, dem)
        evaluate_rg_candidates(fine_candidates, gateway, parameters, dem)
        precompute_ur_coverage(fine_candidates, states, worker_count)
    all_candidates = tuple(level1_candidates) + tuple(fine_candidates)
    final_greedy, final_uncovered_states = weighted_greedy_cover(
        all_candidates, states
    )
    candidate_map = {row.candidate_id: row for row in all_candidates}
    _, uncovered_demand_indices = _demand_coverage_table(
        demands, states, final_greedy, candidate_map
    )

    _validate_grouped_los(all_candidates, demands, states, parameters, gateway, dem)
    if len(level1_uncovered_states):
        print(
            f"Level 1 uncoverable spatial states={len(level1_uncovered_states)}",
            flush=True,
        )
    if len(final_uncovered_states) != 0 and len(uncovered_demand_indices) == 0:
        raise AssertionError("空间状态与原始 demand 的未覆盖判定不一致")

    result = Step4Result(
        demands=demands,
        states=states,
        candidates=all_candidates,
        level1_candidate_count=len(level1_candidates),
        level1_rg_available_count=level1_rg_available,
        level1_greedy=level1_greedy,
        final_greedy=final_greedy,
        uncovered_demand_indices=uncovered_demand_indices,
        runtime_s=0.0,
    )
    if export:
        export_step4(result)

    # 总运行时间包含结果工作簿写出时间。
    runtime_s = perf_counter() - started
    result.runtime_s = runtime_s

    maximum_candidate = max(all_candidates, key=lambda row: row.covered_duration_s)
    print(
        f"Maximum single-point coverage={maximum_candidate.covered_duration_s:.6f}s "
        f"({100.0 * maximum_candidate.covered_duration_s / DEMAND_TOTAL_EXPECTED_S:.6f}%), "
        f"candidate={maximum_candidate.candidate_id}",
        flush=True,
    )
    print(
        f"Final selected positions={len(final_greedy)}, "
        f"covered={DEMAND_TOTAL_EXPECTED_S - sum(demands[i].duration_weight_s for i in uncovered_demand_indices):.6f}s, "
        f"uncovered_demands={len(uncovered_demand_indices)}",
        flush=True,
    )
    for greedy_round in final_greedy:
        candidate = candidate_map[greedy_round.candidate_id]
        print(
            f"{greedy_round.iteration}: {candidate.candidate_id}, "
            f"lon={candidate.longitude_deg:.8f}, lat={candidate.latitude_deg:.8f}, "
            f"h={candidate.hover_height_m:.0f}m, alt={candidate.absolute_altitude_m:.3f}m, "
            f"terrain_clearance={candidate.terrain_clearance_altitude_m:.3f}m, "
            f"actual_flight={candidate.actual_flight_altitude_m:.3f}m, "
            f"new={greedy_round.newly_covered_duration_s:.6f}s, "
            f"candidate_total={candidate.covered_duration_s:.6f}s, "
            f"uavs={candidate.covered_uav_count}",
            flush=True,
        )
    print(f"Runtime={runtime_s:.6f}s", flush=True)
    if export:
        print(f"Result={OUTPUT_FILE}", flush=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workers", type=int, default=DEFAULT_WORKER_COUNT, help="UR 并行进程数"
    )
    parser.add_argument("--no-export", action="store_true")
    arguments = parser.parse_args()
    if arguments.workers < 1:
        raise ValueError("workers 必须至少为 1")
    run_step4(export=not arguments.no_export, worker_count=arguments.workers)


if __name__ == "__main__":
    main()
