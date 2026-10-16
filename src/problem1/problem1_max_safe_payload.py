"""问题1：三种运输无人机执行 O01-Si-O01 单点往返的最大安全载荷。"""

from dataclasses import asdict, dataclass
from pathlib import Path
import sys

# Keep historical module names while resolving all locations from this repository.
_RELEASE_ROOT = Path(__file__).resolve().parents[2]
if str(_RELEASE_ROOT) not in sys.path:
    sys.path.insert(0, str(_RELEASE_ROOT))
from project_paths import ROOT, bootstrap, raw_file, dem_file, input_file, output_dir, output_file, require_file, assert_writable_output

bootstrap()
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd


PROJECT_DIR = ROOT
SEGMENT_FILE = input_file("problem1", 'segment_preprocessing.xlsx')
UAV_FILE = raw_file('运输无人机数据.xlsx')
OUTPUT_FILE = output_file("problem1", 'problem1_max_safe_payload.xlsx')

DEPOT = "O01"
SERVICE_ZONES = [f"S{i:02d}" for i in range(1, 16)]
EXPECTED_UAV_TYPES = ["A", "B", "C"]
GRAVITY_M_S2 = 9.8
JOULES_PER_KWH = 3.6e6
PAYLOAD_TOLERANCE_KG = 1e-8
ENERGY_TOLERANCE_KWH = 1e-9
MONOTONIC_SAMPLE_COUNT = 1001


@dataclass(frozen=True)
class UAVModel:
    """本题能耗模型实际使用的机型参数。"""

    uav_type: str
    model_name: str
    empty_mass_kg: float
    payload_limit_kg: float
    empty_range_m: float
    full_range_m: float
    usable_energy_kwh: float
    reserve_ratio: float
    climb_efficiency: float


@dataclass(frozen=True)
class SegmentEnergy:
    """一个有向航段的水平、爬升及总能耗，单位均为 kWh。"""

    horizontal_energy_kwh: float
    climb_energy_kwh: float
    total_energy_kwh: float


@dataclass(frozen=True)
class RoundTripEnergy:
    """O01-Si-O01 往返能耗分项。"""

    outbound: SegmentEnergy
    return_trip: SegmentEnergy
    total_energy_kwh: float


def load_segment_data(segment_file: Path) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """直接读取预处理结果中的距离矩阵和有向爬升高度矩阵。"""
    distance_matrix = pd.read_excel(
        require_file(segment_file), sheet_name="distance_matrix", index_col=0
    )
    climb_height_matrix = pd.read_excel(
        require_file(segment_file), sheet_name="climb_height_matrix", index_col=0
    )

    expected_nodes = [DEPOT, *SERVICE_ZONES]
    for name, matrix in (
        ("distance_matrix", distance_matrix),
        ("climb_height_matrix", climb_height_matrix),
    ):
        matrix.index = matrix.index.astype(str).str.strip()
        matrix.columns = matrix.columns.astype(str).str.strip()
        if matrix.index.tolist() != expected_nodes or matrix.columns.tolist() != expected_nodes:
            raise ValueError(f"{name} 的节点顺序或节点名称不是 O01、S01-S15")
        if matrix.isna().any().any():
            raise ValueError(f"{name} 中存在空值")
        matrix.loc[:, :] = matrix.astype(float)

    return distance_matrix.astype(float), climb_height_matrix.astype(float)


def load_uav_parameters(uav_file: Path) -> Dict[str, UAVModel]:
    """从附件“三类机型参数”表读取 A/B/C 的指定参数。"""
    raw = pd.read_excel(require_file(uav_file), sheet_name="数据", header=1)
    column_map = {
        "机型编号": "uav_type",
        "机型名称": "model_name",
        "含电池空载总质量（kg）": "empty_mass_kg",
        "最大载货质量（kg）": "payload_limit_kg",
        "空载标准航程（m）": "empty_range_m",
        "满载标准航程（m）": "full_range_m",
        "电池可用能量（kWh）": "usable_energy_kwh",
        "返航电量下限（%）": "reserve_percent",
        "爬升能耗效率": "climb_efficiency",
    }
    missing_columns = [column for column in column_map if column not in raw.columns]
    if missing_columns:
        raise ValueError("运输无人机参数文件缺少字段: " + ", ".join(missing_columns))

    table = raw[list(column_map)].rename(columns=column_map)
    table["uav_type"] = table["uav_type"].astype(str).str.strip().str.upper()
    numeric_columns = [
        "empty_mass_kg",
        "payload_limit_kg",
        "empty_range_m",
        "full_range_m",
        "usable_energy_kwh",
        "reserve_percent",
        "climb_efficiency",
    ]
    # 同一 Sheet 后部还有逐架清单和共享电池表，其中会再次出现 A/B/C；
    # 只有顶部机型参数表同时具备机型名称和本题所需的全部数值字段。
    table = table.loc[
        table["uav_type"].isin(EXPECTED_UAV_TYPES)
        & table["model_name"].notna()
        & table[numeric_columns].notna().all(axis=1)
    ].copy()
    if table["uav_type"].tolist() != EXPECTED_UAV_TYPES:
        raise ValueError("未按 A、B、C 顺序读取到三类运输无人机参数")

    table[numeric_columns] = table[numeric_columns].apply(
        pd.to_numeric, errors="raise"
    )

    models: Dict[str, UAVModel] = {}
    for row in table.itertuples(index=False):
        reserve_ratio = float(row.reserve_percent) / 100.0
        model = UAVModel(
            uav_type=row.uav_type,
            model_name=str(row.model_name).strip(),
            empty_mass_kg=float(row.empty_mass_kg),
            payload_limit_kg=float(row.payload_limit_kg),
            empty_range_m=float(row.empty_range_m),
            full_range_m=float(row.full_range_m),
            usable_energy_kwh=float(row.usable_energy_kwh),
            reserve_ratio=reserve_ratio,
            climb_efficiency=float(row.climb_efficiency),
        )
        if not (
            model.empty_mass_kg > 0
            and model.payload_limit_kg > 0
            and model.empty_range_m >= model.full_range_m > 0
            and model.usable_energy_kwh > 0
            and 0 <= model.reserve_ratio < 1
            and 0 < model.climb_efficiency <= 1
        ):
            raise ValueError(f"机型 {model.uav_type} 存在无效参数: {model}")
        models[model.uav_type] = model
    return models


def equivalent_range(payload_kg: float, model: UAVModel) -> float:
    """L_g(q)：载荷 q 下的等效航程，单位 m。"""
    if payload_kg < -PAYLOAD_TOLERANCE_KG or (
        payload_kg > model.payload_limit_kg + PAYLOAD_TOLERANCE_KG
    ):
        raise ValueError(
            f"机型 {model.uav_type} 的载荷 {payload_kg} kg 超出 "
            f"[0, {model.payload_limit_kg}]"
        )
    payload_kg = min(max(float(payload_kg), 0.0), model.payload_limit_kg)
    payload_ratio = payload_kg / model.payload_limit_kg
    return model.empty_range_m - (
        model.empty_range_m - model.full_range_m
    ) * payload_ratio ** 1.5


def horizontal_energy(
    payload_kg: float, distance_m: float, model: UAVModel
) -> float:
    """水平巡航能耗 E_use*d/L(q)，单位 kWh。"""
    return model.usable_energy_kwh * float(distance_m) / equivalent_range(
        payload_kg, model
    )


def climb_energy(
    payload_kg: float, climb_height_m: float, model: UAVModel
) -> float:
    """爬升附加能耗 (m+q)g0*h/(eta*3.6e6)，单位 kWh。"""
    return (
        (model.empty_mass_kg + float(payload_kg))
        * GRAVITY_M_S2
        * float(climb_height_m)
        / (model.climb_efficiency * JOULES_PER_KWH)
    )


def segment_energy(
    payload_kg: float,
    distance_m: float,
    climb_height_m: float,
    model: UAVModel,
) -> SegmentEnergy:
    """返回单个有向航段的水平、爬升和总能耗。"""
    horizontal = horizontal_energy(payload_kg, distance_m, model)
    climb = climb_energy(payload_kg, climb_height_m, model)
    return SegmentEnergy(horizontal, climb, horizontal + climb)


def round_trip_energy(
    payload_kg: float,
    outbound_distance_m: float,
    return_distance_m: float,
    outbound_climb_height_m: float,
    return_climb_height_m: float,
    model: UAVModel,
) -> RoundTripEnergy:
    """去程携带 q、返程空载时的 O01-Si-O01 总运输能耗。"""
    outbound = segment_energy(
        payload_kg, outbound_distance_m, outbound_climb_height_m, model
    )
    return_trip = segment_energy(
        0.0, return_distance_m, return_climb_height_m, model
    )
    return RoundTripEnergy(
        outbound=outbound,
        return_trip=return_trip,
        total_energy_kwh=outbound.total_energy_kwh + return_trip.total_energy_kwh,
    )


def verify_energy_monotonicity(
    model: UAVModel,
    service_zone: str,
    outbound_distance_m: float,
    return_distance_m: float,
    outbound_climb_height_m: float,
    return_climb_height_m: float,
) -> bool:
    """在 [0,Q] 上以 1001 点数值检查 E_T(q) 单调非减。"""
    payloads = np.linspace(0.0, model.payload_limit_kg, MONOTONIC_SAMPLE_COUNT)
    energies = np.array(
        [
            round_trip_energy(
                q,
                outbound_distance_m,
                return_distance_m,
                outbound_climb_height_m,
                return_climb_height_m,
                model,
            ).total_energy_kwh
            for q in payloads
        ]
    )
    violation_indices = np.flatnonzero(np.diff(energies) < -ENERGY_TOLERANCE_KWH)
    for index in violation_indices:
        print(
            "单调性违反: "
            f"机型={model.uav_type}, 服务区={service_zone}, "
            f"q1={payloads[index]:.9f} kg, E_T(q1)={energies[index]:.12f} kWh, "
            f"q2={payloads[index + 1]:.9f} kg, "
            f"E_T(q2)={energies[index + 1]:.12f} kWh"
        )
    return len(violation_indices) == 0


def solve_max_safe_payload(
    model: UAVModel,
    outbound_distance_m: float,
    return_distance_m: float,
    outbound_climb_height_m: float,
    return_climb_height_m: float,
) -> Tuple[float, bool, str, RoundTripEnergy, RoundTripEnergy]:
    """按“先满载、再空载、最后二分”的顺序求最大安全载荷。"""
    energy_limit = (1.0 - model.reserve_ratio) * model.usable_energy_kwh

    def energy_at(payload_kg: float) -> RoundTripEnergy:
        return round_trip_energy(
            payload_kg,
            outbound_distance_m,
            return_distance_m,
            outbound_climb_height_m,
            return_climb_height_m,
            model,
        )

    empty_energy = energy_at(0.0)
    full_energy = energy_at(model.payload_limit_kg)
    if full_energy.total_energy_kwh <= energy_limit + ENERGY_TOLERANCE_KWH:
        return (
            model.payload_limit_kg,
            True,
            "满载可安全往返",
            empty_energy,
            full_energy,
        )
    if empty_energy.total_energy_kwh > energy_limit + ENERGY_TOLERANCE_KWH:
        return (
            float("nan"),
            False,
            "空载往返亦不满足返航安全余量",
            empty_energy,
            full_energy,
        )

    low, high = 0.0, model.payload_limit_kg
    while high - low > PAYLOAD_TOLERANCE_KG:
        middle = (low + high) / 2.0
        if energy_at(middle).total_energy_kwh <= energy_limit:
            low = middle
        else:
            high = middle
    return low, True, "受能量约束限制", empty_energy, full_energy


def calculate_all_payloads(
    distance_matrix: pd.DataFrame,
    climb_height_matrix: pd.DataFrame,
    models: Dict[str, UAVModel],
) -> pd.DataFrame:
    """计算 3×15 个最大安全载荷及其能耗分项。"""
    records: List[dict] = []
    monotonic_failures = []

    for uav_type in EXPECTED_UAV_TYPES:
        model = models[uav_type]
        energy_limit = (1.0 - model.reserve_ratio) * model.usable_energy_kwh
        for service_zone in SERVICE_ZONES:
            outbound_distance = float(distance_matrix.loc[DEPOT, service_zone])
            return_distance = float(distance_matrix.loc[service_zone, DEPOT])
            outbound_climb = float(climb_height_matrix.loc[DEPOT, service_zone])
            return_climb = float(climb_height_matrix.loc[service_zone, DEPOT])

            monotonic = verify_energy_monotonicity(
                model,
                service_zone,
                outbound_distance,
                return_distance,
                outbound_climb,
                return_climb,
            )
            if not monotonic:
                monotonic_failures.append(f"{uav_type}-{service_zone}")

            (
                max_payload,
                feasible,
                status,
                empty_round_trip,
                full_round_trip,
            ) = solve_max_safe_payload(
                model,
                outbound_distance,
                return_distance,
                outbound_climb,
                return_climb,
            )

            if feasible:
                result_energy = round_trip_energy(
                    max_payload,
                    outbound_distance,
                    return_distance,
                    outbound_climb,
                    return_climb,
                    model,
                )
                outbound = result_energy.outbound
                return_trip = result_energy.return_trip
                total_energy = result_energy.total_energy_kwh
                remaining_energy = model.usable_energy_kwh - total_energy
                remaining_ratio = remaining_energy / model.usable_energy_kwh
            else:
                outbound = SegmentEnergy(np.nan, np.nan, np.nan)
                return_trip = SegmentEnergy(np.nan, np.nan, np.nan)
                total_energy = np.nan
                remaining_energy = np.nan
                remaining_ratio = np.nan

            records.append(
                {
                    "uav_type": uav_type,
                    "service_zone": service_zone,
                    "max_payload_kg": max_payload,
                    "feasible": feasible,
                    "status": status,
                    "payload_limit_kg": model.payload_limit_kg,
                    "energy_limit_kwh": energy_limit,
                    "distance_m": outbound_distance,
                    "outbound_distance_m": outbound_distance,
                    "return_distance_m": return_distance,
                    "outbound_climb_height_m": outbound_climb,
                    "return_climb_height_m": return_climb,
                    "outbound_horizontal_energy_kwh": outbound.horizontal_energy_kwh,
                    "outbound_climb_energy_kwh": outbound.climb_energy_kwh,
                    "outbound_total_energy_kwh": outbound.total_energy_kwh,
                    "return_horizontal_energy_kwh": return_trip.horizontal_energy_kwh,
                    "return_climb_energy_kwh": return_trip.climb_energy_kwh,
                    "return_total_energy_kwh": return_trip.total_energy_kwh,
                    "round_trip_energy_kwh": total_energy,
                    "remaining_energy_kwh": remaining_energy,
                    "remaining_energy_ratio": remaining_ratio,
                    "empty_round_trip_energy_kwh": empty_round_trip.total_energy_kwh,
                    "full_round_trip_energy_kwh": full_round_trip.total_energy_kwh,
                    "monotonic_non_decreasing": monotonic,
                }
            )

    if monotonic_failures:
        raise RuntimeError("以下组合的总能耗未通过单调性检查: " + ", ".join(monotonic_failures))
    print(f"\n[通过] 45 个组合均通过 {MONOTONIC_SAMPLE_COUNT} 点能耗单调性检查")
    return pd.DataFrame(records)


def validate_results(
    results: pd.DataFrame,
    distance_matrix: pd.DataFrame,
    models: Dict[str, UAVModel],
) -> None:
    """执行题目指定的端点、可行性、边界和能量约束检查。"""
    checks = {}
    checks["得到 3×15=45 个机型-服务区组合"] = len(results) == 45

    range_at_empty_ok = True
    range_at_full_ok = True
    for model in models.values():
        range_at_empty_ok &= np.isclose(
            equivalent_range(0.0, model), model.empty_range_m, atol=1e-12
        )
        range_at_full_ok &= np.isclose(
            equivalent_range(model.payload_limit_kg, model),
            model.full_range_m,
            atol=1e-12,
        )
    checks["所有机型 L_g(0)=L_g^0"] = bool(range_at_empty_ok)
    checks["所有机型 L_g(Q_g)=L_g^F"] = bool(range_at_full_ok)

    feasible = results.loc[results["feasible"]].copy()
    infeasible = results.loc[~results["feasible"]].copy()
    checks["所有可行载荷均位于 [0,Q_g]"] = bool(
        (
            (feasible["max_payload_kg"] >= -PAYLOAD_TOLERANCE_KG)
            & (
                feasible["max_payload_kg"]
                <= feasible["payload_limit_kg"] + PAYLOAD_TOLERANCE_KG
            )
        ).all()
    )
    checks["所有可行最大载荷均满足安全能量约束"] = bool(
        (
            feasible["round_trip_energy_kwh"]
            <= feasible["energy_limit_kwh"] + ENERGY_TOLERANCE_KWH
        ).all()
    )

    constrained = feasible.loc[
        feasible["max_payload_kg"]
        < feasible["payload_limit_kg"] - PAYLOAD_TOLERANCE_KG
    ]
    closeness_tolerance = np.maximum(
        1e-8, constrained["energy_limit_kwh"].to_numpy() * 1e-8
    )
    checks["受限组合在最大载荷处能耗紧邻安全上限"] = bool(
        np.all(
            np.abs(
                constrained["round_trip_energy_kwh"].to_numpy()
                - constrained["energy_limit_kwh"].to_numpy()
            )
            <= closeness_tolerance
        )
    )

    full_load = feasible.loc[
        np.isclose(
            feasible["max_payload_kg"],
            feasible["payload_limit_kg"],
            atol=PAYLOAD_TOLERANCE_KG,
        )
    ]
    checks["满载组合在 Q_g 处满足安全能量约束"] = bool(
        (
            full_load["full_round_trip_energy_kwh"]
            <= full_load["energy_limit_kwh"] + ENERGY_TOLERANCE_KWH
        ).all()
    )
    checks["不可行组合的空载能耗严格超过安全上限"] = bool(
        (
            infeasible["empty_round_trip_energy_kwh"]
            > infeasible["energy_limit_kwh"] + ENERGY_TOLERANCE_KWH
        ).all()
    )
    checks["O01-Si 与 Si-O01 水平距离相等"] = bool(
        all(
            np.isclose(
                distance_matrix.loc[DEPOT, service_zone],
                distance_matrix.loc[service_zone, DEPOT],
                atol=1e-9,
            )
            for service_zone in SERVICE_ZONES
        )
    )
    checks["45 个组合均通过数值单调性验证"] = bool(
        results["monotonic_non_decreasing"].all()
    )

    print("\n结果自动检查:")
    for description, passed in checks.items():
        print(f"[{'通过' if passed else '失败'}] {description}")
    failures = [description for description, passed in checks.items() if not passed]
    if failures:
        raise RuntimeError("结果检查失败: " + "; ".join(failures))


def uav_parameter_frame(models: Dict[str, UAVModel]) -> pd.DataFrame:
    """构造写入结果工作簿的实际机型参数表。"""
    rows = []
    for uav_type in EXPECTED_UAV_TYPES:
        row = asdict(models[uav_type])
        rows.append(row)
    return pd.DataFrame(rows)[
        [
            "uav_type",
            "model_name",
            "empty_mass_kg",
            "payload_limit_kg",
            "empty_range_m",
            "full_range_m",
            "usable_energy_kwh",
            "reserve_ratio",
            "climb_efficiency",
        ]
    ]


def save_results(
    output_file: Path, results: pd.DataFrame, models: Dict[str, UAVModel]
) -> None:
    """保存明细、3×15 载荷矩阵和实际使用的机型参数。"""
    output_file.parent.mkdir(parents=True, exist_ok=True)
    payload_matrix = results.pivot(
        index="uav_type", columns="service_zone", values="max_payload_kg"
    ).reindex(index=EXPECTED_UAV_TYPES, columns=SERVICE_ZONES)
    payload_matrix.index.name = "uav_type"

    with pd.ExcelWriter(assert_writable_output(output_file), engine="openpyxl") as writer:
        results.to_excel(writer, sheet_name="max_safe_payload", index=False)
        payload_matrix.to_excel(writer, sheet_name="payload_matrix")
        uav_parameter_frame(models).to_excel(
            writer, sheet_name="uav_parameters", index=False
        )

        for worksheet in writer.book.worksheets:
            worksheet.freeze_panes = "A2"
            for column_cells in worksheet.iter_cols():
                values = ["" if cell.value is None else str(cell.value) for cell in column_cells]
                width = min(max(max(map(len, values)) + 2, 12), 38)
                worksheet.column_dimensions[column_cells[0].column_letter].width = width
                for cell in column_cells[1:]:
                    if isinstance(cell.value, float):
                        cell.number_format = "0.000000000"


def print_uav_parameters(models: Dict[str, UAVModel]) -> None:
    """打印程序从附件实际读取的三种机型参数。"""
    display = uav_parameter_frame(models).rename(
        columns={
            "uav_type": "机型",
            "model_name": "机型名称",
            "empty_mass_kg": "m_g/kg",
            "payload_limit_kg": "Q_g/kg",
            "empty_range_m": "L_g^0/m",
            "full_range_m": "L_g^F/m",
            "usable_energy_kwh": "E_g^use/kWh",
            "reserve_ratio": "rho_g",
            "climb_efficiency": "eta_g^up",
        }
    )
    print("\n三种运输无人机实际读取参数:")
    print(display.to_string(index=False))
    print(f"统一重力加速度 g_0 = {GRAVITY_M_S2} m/s^2")


def print_result_summary(results: pd.DataFrame) -> None:
    """打印 45 个组合的简洁结果表。"""
    summary = results[
        [
            "uav_type",
            "service_zone",
            "max_payload_kg",
            "round_trip_energy_kwh",
            "energy_limit_kwh",
            "remaining_energy_ratio",
        ]
    ].copy()
    summary.columns = [
        "机型",
        "服务区",
        "最大安全载荷/kg",
        "往返能耗/kWh",
        "安全能量上限/kWh",
        "剩余能量比例",
    ]
    print("\n45 个最大安全载荷结果:")
    print(
        summary.to_string(
            index=False,
            na_rep="不可行",
            formatters={
                "最大安全载荷/kg": lambda value: f"{value:.6f}",
                "往返能耗/kWh": lambda value: f"{value:.9f}",
                "安全能量上限/kWh": lambda value: f"{value:.6f}",
                "剩余能量比例": lambda value: f"{value:.6%}",
            },
        )
    )


def print_a_s01_detail(results: pd.DataFrame, models: Dict[str, UAVModel]) -> None:
    """打印 A 型-S01 的完整公式核算分项。"""
    row = results.loc[
        (results["uav_type"] == "A") & (results["service_zone"] == "S01")
    ].iloc[0]
    model = models["A"]
    print("\nA 型无人机 → S01 完整核算:")
    print(f"d_O01,S01 = {row.outbound_distance_m:.9f} m")
    print(f"d_S01,O01 = {row.return_distance_m:.9f} m")
    print(f"h_O01,S01^+ = {row.outbound_climb_height_m:.9f} m")
    print(f"h_S01,O01^+ = {row.return_climb_height_m:.9f} m")
    if not row.feasible:
        print("空载往返亦不满足返航安全余量，无最大安全载荷。")
        return
    print(f"q_max = {row.max_payload_kg:.9f} kg")
    print(f"L_A(q_max) = {equivalent_range(row.max_payload_kg, model):.9f} m")
    print(f"去程水平能耗 = {row.outbound_horizontal_energy_kwh:.12f} kWh")
    print(f"去程爬升能耗 = {row.outbound_climb_energy_kwh:.12f} kWh")
    print(f"去程总能耗 = {row.outbound_total_energy_kwh:.12f} kWh")
    print(f"返程水平能耗 = {row.return_horizontal_energy_kwh:.12f} kWh")
    print(f"返程爬升能耗 = {row.return_climb_energy_kwh:.12f} kWh")
    print(f"返程总能耗 = {row.return_total_energy_kwh:.12f} kWh")
    print(f"往返总能耗 = {row.round_trip_energy_kwh:.12f} kWh")
    print(f"安全能量上限 = {row.energy_limit_kwh:.12f} kWh")
    print(f"剩余电量比例 = {row.remaining_energy_ratio:.9%}")


def main() -> None:
    print(f"运输无人机参数文件: {UAV_FILE}")
    print(f"航段预处理文件: {SEGMENT_FILE}")
    print(f"结果输出文件: {OUTPUT_FILE}")

    models = load_uav_parameters(UAV_FILE)
    print_uav_parameters(models)
    distance_matrix, climb_height_matrix = load_segment_data(SEGMENT_FILE)
    results = calculate_all_payloads(distance_matrix, climb_height_matrix, models)
    validate_results(results, distance_matrix, models)
    save_results(OUTPUT_FILE, results, models)
    print_result_summary(results)
    print_a_s01_detail(results, models)

    feasible_count = int(results["feasible"].sum())
    print(f"\n成功计算组合数: {len(results)}（可行 {feasible_count}，不可行 {len(results) - feasible_count}）")
    print(f"Excel 已保存: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
