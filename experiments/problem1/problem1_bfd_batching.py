"""问题1第一层：基于改进 BFD 贪心策略的初始货箱组批。"""

from collections import Counter
from pathlib import Path
import sys

# Keep historical module names while resolving all locations from this repository.
_RELEASE_ROOT = Path(__file__).resolve().parents[2]
if str(_RELEASE_ROOT) not in sys.path:
    sys.path.insert(0, str(_RELEASE_ROOT))
from project_paths import ROOT, bootstrap, raw_file, dem_file, input_file, output_dir, output_file, require_file, assert_writable_output

bootstrap()
from time import perf_counter
from typing import Dict, List, Tuple

import pandas as pd


PROJECT_DIR = ROOT
DATA_DIR = raw_file("运输无人机数据.xlsx").parent
BOX_FILE = raw_file('物资需求与配送时限.xlsx')
UAV_FILE = raw_file('运输无人机数据.xlsx')
QMAX_FILE = input_file("problem1", 'problem1_max_safe_payload.xlsx')
OUTPUT_FILE = output_file("problem1", 'problem1_bfd_batching.xlsx')

SERVICE_ZONES = [f"S{i:03d}" for i in range(1, 16)]
UAV_TYPES = ["A", "B", "C"]

# 本阶段严格使用题目指定的可用装载体积，而不是其他额定体积。
REQUIRED_VOLUME_LIMITS = {"A": 0.060, "B": 0.073, "C": 0.250}
CAPACITY_TOLERANCE = 1e-10


def to_box_zone(service_zone: str) -> str:
    """将 q_max 文件中的 S01-S15 统一成货箱文件中的 S001-S015。"""
    return f"S{int(str(service_zone).strip()[1:]):03d}"


def load_box_data(box_file: Path) -> pd.DataFrame:
    """读取附件已有的逐箱货箱数据和货箱编号。"""
    raw = pd.read_excel(require_file(box_file), sheet_name="逐箱货箱清单")
    column_map = {
        "货箱编号": "box_id",
        "服务区编号": "service_zone",
        "物资类型": "material_type",
        "单箱质量（kg）": "weight_kg",
        "单箱体积（m³）": "volume_m3",
    }
    boxes = raw[list(column_map)].rename(columns=column_map).copy()
    boxes["box_id"] = boxes["box_id"].astype(str).str.strip()
    boxes["service_zone"] = boxes["service_zone"].astype(str).str.strip().str.upper()
    boxes["material_type"] = boxes["material_type"].astype(str).str.strip()
    boxes[["weight_kg", "volume_m3"]] = boxes[
        ["weight_kg", "volume_m3"]
    ].apply(pd.to_numeric, errors="raise")
    return boxes


def load_uav_parameters(uav_file: Path) -> Dict[str, float]:
    """读取附件机型表，并采用指定的 A/B/C 可用装载体积。"""
    raw = pd.read_excel(require_file(uav_file), sheet_name="数据", header=1)
    table = raw[["机型编号", "可用装载体积（m³）"]].rename(
        columns={
            "机型编号": "uav_type",
            "可用装载体积（m³）": "max_volume_m3",
        }
    )
    table["uav_type"] = table["uav_type"].astype(str).str.strip().str.upper()
    table = table.loc[table["uav_type"].isin(UAV_TYPES)].copy()
    table["max_volume_m3"] = pd.to_numeric(
        table["max_volume_m3"], errors="raise"
    )
    table = table.drop_duplicates("uav_type").set_index("uav_type").loc[UAV_TYPES]

    actual = table["max_volume_m3"].astype(float).to_dict()
    for uav_type in UAV_TYPES:
        if abs(actual[uav_type] - REQUIRED_VOLUME_LIMITS[uav_type]) > 1e-12:
            raise ValueError(
                f"机型 {uav_type} 的附件可用装载体积与指定值不一致: "
                f"{actual[uav_type]} != {REQUIRED_VOLUME_LIMITS[uav_type]}"
            )
    return REQUIRED_VOLUME_LIMITS.copy()


def load_qmax_data(qmax_file: Path) -> Dict[Tuple[str, str], float]:
    """直接读取已有最大安全载荷结果，不重复计算能耗或 q_max。"""
    table = pd.read_excel(require_file(qmax_file), sheet_name="max_safe_payload")
    table = table.loc[table["feasible"]].copy()
    qmax = {
        (str(row.uav_type).strip().upper(), to_box_zone(row.service_zone)): float(
            row.max_payload_kg
        )
        for row in table.itertuples(index=False)
    }
    expected = {
        (uav_type, service_zone)
        for uav_type in UAV_TYPES
        for service_zone in SERVICE_ZONES
    }
    if set(qmax) != expected:
        raise ValueError("q_max 结果未包含全部 3×15 个可行的机型-服务区组合")
    return qmax


def get_feasible_types(
    service_zone: str,
    total_weight_kg: float,
    total_volume_m3: float,
    qmax: Dict[Tuple[str, str], float],
    volume_limits: Dict[str, float],
) -> List[str]:
    """按 A、B、C 固定顺序返回能够运输给定装载组合的机型。"""
    return [
        uav_type
        for uav_type in UAV_TYPES
        if total_weight_kg
        <= qmax[(uav_type, service_zone)] + CAPACITY_TOLERANCE
        and total_volume_m3
        <= volume_limits[uav_type] + CAPACITY_TOLERANCE
    ]


def compute_residual_capacity(
    total_weight_kg: float,
    total_volume_m3: float,
    uav_type: str,
    service_zone: str,
    qmax: Dict[Tuple[str, str], float],
    volume_limits: Dict[str, float],
) -> float:
    """计算 R=2-W/q_max-V/V_g，R 越小表示容量匹配越紧凑。"""
    return (
        2.0
        - total_weight_kg / qmax[(uav_type, service_zone)]
        - total_volume_m3 / volume_limits[uav_type]
    )


def compute_item_difficulty(
    boxes: pd.DataFrame,
    qmax: Dict[Tuple[str, str], float],
    volume_limits: Dict[str, float],
) -> pd.DataFrame:
    """计算 eta_ij，并按 eta 降序、质量降序、货箱编号升序排序。"""
    records = []
    for box in boxes.itertuples(index=False):
        feasible_types = get_feasible_types(
            box.service_zone,
            float(box.weight_kg),
            float(box.volume_m3),
            qmax,
            volume_limits,
        )
        if not feasible_types:
            raise ValueError(f"货箱 {box.box_id} 无任何可单独运输的机型")

        # eta_ijg 取质量、体积利用率中的较大者；eta_ij 再取可行机型中的最小值。
        eta_by_type = {
            uav_type: max(
                float(box.weight_kg) / qmax[(uav_type, box.service_zone)],
                float(box.volume_m3) / volume_limits[uav_type],
            )
            for uav_type in feasible_types
        }
        records.append(
            {
                "service_zone": box.service_zone,
                "box_id": box.box_id,
                "material_type": box.material_type,
                "weight_kg": float(box.weight_kg),
                "volume_m3": float(box.volume_m3),
                "eta": min(eta_by_type.values()),
                "eta_A": eta_by_type.get("A"),
                "eta_B": eta_by_type.get("B"),
                "eta_C": eta_by_type.get("C"),
            }
        )

    sorted_boxes = pd.DataFrame(records).sort_values(
        ["service_zone", "eta", "weight_kg", "box_id"],
        ascending=[True, False, False, True],
        kind="stable",
    )
    sorted_boxes["sort_order"] = (
        sorted_boxes.groupby("service_zone", sort=False).cumcount() + 1
    )
    return sorted_boxes.reset_index(drop=True)


def bfd_batch_service_zone(
    service_zone: str,
    sorted_zone_boxes: pd.DataFrame,
    qmax: Dict[Tuple[str, str], float],
    volume_limits: Dict[str, float],
) -> List[dict]:
    """对一个服务区执行动态改进 BFD，批次数由算法自行产生。"""
    batches: List[dict] = []

    for box in sorted_zone_boxes.itertuples(index=False):
        feasible_combinations = []

        # 枚举所有已有批次及三种机型；此处机型只用于本次 Best-Fit 判断，不锁定批次。
        for batch_index, batch in enumerate(batches):
            new_weight = batch["total_weight_kg"] + box.weight_kg
            new_volume = batch["total_volume_m3"] + box.volume_m3
            for type_order, uav_type in enumerate(UAV_TYPES):
                if (
                    new_weight
                    <= qmax[(uav_type, service_zone)] + CAPACITY_TOLERANCE
                    and new_volume
                    <= volume_limits[uav_type] + CAPACITY_TOLERANCE
                ):
                    residual = compute_residual_capacity(
                        new_weight,
                        new_volume,
                        uav_type,
                        service_zone,
                        qmax,
                        volume_limits,
                    )
                    feasible_combinations.append(
                        (residual, batch_index, type_order)
                    )

        if feasible_combinations:
            # R 最小时优先；完全并列时按批次建立顺序、A/B/C 顺序确定，保证可复现。
            _, best_batch_index, _ = min(feasible_combinations)
            best_batch = batches[best_batch_index]
            best_batch["box_ids"].append(box.box_id)
            best_batch["total_weight_kg"] += box.weight_kg
            best_batch["total_volume_m3"] += box.volume_m3
        else:
            # 没有可行的已有 (B,g) 组合时，以当前不可拆分货箱新建批次。
            batches.append(
                {
                    "service_zone": service_zone,
                    "box_ids": [box.box_id],
                    "total_weight_kg": float(box.weight_kg),
                    "total_volume_m3": float(box.volume_m3),
                }
            )

    return batches


def assign_final_uav_type(
    batch: dict,
    qmax: Dict[Tuple[str, str], float],
    volume_limits: Dict[str, float],
) -> Tuple[str, float]:
    """组批结束后重新枚举可行机型，选择 R 最小者，完全并列时按 A-B-C。"""
    service_zone = batch["service_zone"]
    feasible_types = get_feasible_types(
        service_zone,
        batch["total_weight_kg"],
        batch["total_volume_m3"],
        qmax,
        volume_limits,
    )
    if not feasible_types:
        raise RuntimeError(f"{service_zone} 存在无可行机型的最终批次")

    choices = [
        (
            compute_residual_capacity(
                batch["total_weight_kg"],
                batch["total_volume_m3"],
                uav_type,
                service_zone,
                qmax,
                volume_limits,
            ),
            UAV_TYPES.index(uav_type),
            uav_type,
        )
        for uav_type in feasible_types
    ]
    residual, _, selected_type = min(choices)
    return selected_type, residual


def build_batch_results(
    all_batches: List[dict],
    qmax: Dict[Tuple[str, str], float],
    volume_limits: Dict[str, float],
) -> pd.DataFrame:
    """确定每个最终批次的机型，并构造结果表。"""
    zone_batch_number = Counter()
    records = []
    for batch in all_batches:
        service_zone = batch["service_zone"]
        zone_batch_number[service_zone] += 1
        uav_type, residual = assign_final_uav_type(batch, qmax, volume_limits)
        max_payload = qmax[(uav_type, service_zone)]
        max_volume = volume_limits[uav_type]
        weight = batch["total_weight_kg"]
        volume = batch["total_volume_m3"]
        records.append(
            {
                "batch_id": f"BFD-{service_zone}-{zone_batch_number[service_zone]:02d}",
                "service_zone": service_zone,
                "uav_type": uav_type,
                "box_count": len(batch["box_ids"]),
                "box_ids": ", ".join(batch["box_ids"]),
                "total_weight_kg": weight,
                "total_volume_m3": volume,
                "max_safe_payload_kg": max_payload,
                "max_volume_m3": max_volume,
                "weight_utilization": weight / max_payload,
                "volume_utilization": volume / max_volume,
                "weight_remaining_kg": max_payload - weight,
                "volume_remaining_m3": max_volume - volume,
                "residual_capacity_score": residual,
                "_box_ids": tuple(batch["box_ids"]),
            }
        )
    return pd.DataFrame(records)


def validate_solution(
    boxes: pd.DataFrame,
    batch_results: pd.DataFrame,
    qmax: Dict[Tuple[str, str], float],
    volume_limits: Dict[str, float],
) -> None:
    """验证完整覆盖、唯一分配、同区组批及最终机型容量可行性。"""
    assigned_ids = [
        box_id for box_ids in batch_results["_box_ids"] for box_id in box_ids
    ]
    counts = Counter(assigned_ids)
    input_ids = set(boxes["box_id"])
    box_zones = boxes.set_index("box_id")["service_zone"].to_dict()

    all_boxes_present = set(assigned_ids) == input_ids
    each_box_once = (
        len(assigned_ids) == len(boxes)
        and all(counts[box_id] == 1 for box_id in input_ids)
    )
    same_zone = all(
        all(box_zones[box_id] == service_zone for box_id in box_ids)
        for service_zone, box_ids in zip(
            batch_results["service_zone"], batch_results["_box_ids"]
        )
    )
    weight_feasible = bool(
        (
            batch_results["total_weight_kg"]
            <= batch_results["max_safe_payload_kg"] + CAPACITY_TOLERANCE
        ).all()
    )
    volume_feasible = bool(
        (
            batch_results["total_volume_m3"]
            <= batch_results["max_volume_m3"] + CAPACITY_TOLERANCE
        ).all()
    )
    nonempty_feasible_sets = all(
        bool(
            get_feasible_types(
                row.service_zone,
                row.total_weight_kg,
                row.total_volume_m3,
                qmax,
                volume_limits,
            )
        )
        for row in batch_results.itertuples(index=False)
    )

    checks = [
        ("所有输入货箱均出现在最终组批结果中", all_boxes_present),
        ("每个货箱恰好出现一次", each_box_once),
        ("所有批次均未跨服务区组批", same_zone),
        ("所有批次总质量均不超过最终机型 q_max", weight_feasible),
        ("所有批次总体积均不超过最终机型可用体积", volume_feasible),
        ("每个最终批次的可行机型集合均非空", nonempty_feasible_sets),
    ]
    for description, passed in checks:
        print(f"[{'通过' if passed else '失败'}] {description}")
    failures = [description for description, passed in checks if not passed]
    if failures:
        raise RuntimeError("BFD 初始组批方案验证失败: " + "; ".join(failures))
    print("BFD初始组批方案验证通过")


def save_results(
    output_file: Path,
    batch_results: pd.DataFrame,
    sorted_boxes: pd.DataFrame,
) -> None:
    """输出最终批次与逐服务区货箱难度排序结果。"""
    output_file.parent.mkdir(parents=True, exist_ok=True)
    exported_batches = batch_results.drop(columns="_box_ids")
    with pd.ExcelWriter(assert_writable_output(output_file), engine="openpyxl") as writer:
        exported_batches.to_excel(writer, sheet_name="BFD组批结果", index=False)
        sorted_boxes.to_excel(writer, sheet_name="货箱排序结果", index=False)

        for worksheet in writer.book.worksheets:
            worksheet.freeze_panes = "A2"
            worksheet.auto_filter.ref = worksheet.dimensions
            for column_cells in worksheet.iter_cols():
                values = [
                    "" if cell.value is None else str(cell.value)
                    for cell in column_cells
                ]
                width = min(max(max(map(len, values)) + 2, 12), 60)
                worksheet.column_dimensions[column_cells[0].column_letter].width = width
                for cell in column_cells[1:]:
                    if isinstance(cell.value, float):
                        cell.number_format = "0.000000000"


def print_summary(batch_results: pd.DataFrame, runtime_s: float) -> None:
    """打印各服务区、各机型批次数及总运行时间。"""
    zone_counts = batch_results["service_zone"].value_counts()
    print("\nBFD 初始组批结果摘要")
    for service_zone in SERVICE_ZONES:
        count = int(zone_counts.get(service_zone, 0))
        unit = "batch" if count == 1 else "batches"
        print(f"{service_zone}: {count} {unit}")
    print(f"\nTotal batches: {len(batch_results)}")

    type_counts = batch_results["uav_type"].value_counts()
    for uav_type in UAV_TYPES:
        print(f"{uav_type}型批次数: {int(type_counts.get(uav_type, 0))}")
    print(f"程序总运行时间: {runtime_s:.6f} s")


def main() -> None:
    program_start = perf_counter()
    boxes = load_box_data(BOX_FILE)
    volume_limits = load_uav_parameters(UAV_FILE)
    qmax = load_qmax_data(QMAX_FILE)

    sorted_boxes = compute_item_difficulty(boxes, qmax, volume_limits)
    all_batches = []
    for service_zone in SERVICE_ZONES:
        zone_boxes = sorted_boxes.loc[
            sorted_boxes["service_zone"] == service_zone
        ]
        all_batches.extend(
            bfd_batch_service_zone(
                service_zone, zone_boxes, qmax, volume_limits
            )
        )

    batch_results = build_batch_results(all_batches, qmax, volume_limits)
    validate_solution(boxes, batch_results, qmax, volume_limits)
    save_results(OUTPUT_FILE, batch_results, sorted_boxes)
    total_runtime = perf_counter() - program_start
    print_summary(batch_results, total_runtime)
    print(f"输出文件: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
