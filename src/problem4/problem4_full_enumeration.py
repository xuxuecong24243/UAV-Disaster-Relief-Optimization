"""Problem 4 exact K=2/K=3 enumeration built on the validated Phase 1--6 logic."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import sys
from time import perf_counter
from typing import Dict, Iterable, List, Mapping, MutableMapping, Sequence, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from project_paths import bootstrap, output_dir

bootstrap()

import problem4_service_zone_graph as phase123
from problem4_group_scheduler import (
    INTERVAL_PARTITION_CALLS,
    ResourceAccountingContext,
    SubsetResourceResult,
    _battery_times_fixed,
    _gamma_intersections_correct,
    _no_overlap,
    _relay_times_fixed,
    _transport_times_fixed,
    account_subset,
    load_context,
)


MODULE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = output_dir("problem4")
RUN_LOG = OUTPUT_DIR / "problem4_run.log"
K2_ALL_OUTPUT = OUTPUT_DIR / "problem4_K2_all.xlsx"
K3_ALL_OUTPUT = OUTPUT_DIR / "problem4_K3_all.xlsx"
K2_PARETO_OUTPUT = OUTPUT_DIR / "problem4_K2_pareto.xlsx"
K3_PARETO_OUTPUT = OUTPUT_DIR / "problem4_K3_pareto.xlsx"
SUBSET_OUTPUT = OUTPUT_DIR / "group_subset_results.xlsx"
RELAY_DUPLICATION_OUTPUT = OUTPUT_DIR / "relay_duplication_analysis.xlsx"
CANDIDATE_OUTPUT = OUTPUT_DIR / "problem4_representative.xlsx"

BASELINE_UAV = {"A": 4, "B": 2, "C": 2}
BASELINE_BATTERY = {"A": 6, "B": 4, "C": 4}
BASELINE_RELAY_UAV = 2
BASELINE_RELAY_ENERGY = 3
INVENTORY_RELAY_ENERGY = 6

RESOURCE_COLUMNS = [
    "R_U_A",
    "R_U_B",
    "R_U_C",
    "R_B_A",
    "R_B_B",
    "R_B_C",
    "R_R",
    "R_E",
]
BALANCE_COLUMNS = [
    "B_Q",
    "B_M",
    "B_V",
    "B_N_sortie",
    "B_T_transport",
    "B_T_comm",
]
PARETO_COLUMNS = RESOURCE_COLUMNS + BALANCE_COLUMNS
RELAY_IDS = ("RS001", "RS002", "RS003")
NUMERIC_TOLERANCE = 1e-10


def _joined(values: Iterable[object]) -> str:
    return ";".join(str(value) for value in values)


def _format_assignment(table: pd.DataFrame, scope: str = "") -> str:
    if table.empty:
        return ""
    rows = []
    for row in table.sort_values(["start_s", "task_id"]).itertuples(index=False):
        resource_id = f"{scope}::{row.resource_id}" if scope else str(row.resource_id)
        rows.append(f"{row.task_id}->{resource_id}")
    return ";".join(rows)


def _communication_duration_by_sortie(
    context: ResourceAccountingContext,
) -> Dict[str, float]:
    """Sum every Problem-3 communication interval, including direct and relay service."""
    table = context.communication_intervals
    sortie_column = table.columns[0]
    return {
        str(sortie_id): float(duration)
        for sortie_id, duration in table.groupby(sortie_column)["Duration_s"].sum().items()
    }


def _subset_t_comm(
    result: SubsetResourceResult,
    communication_by_sortie: Mapping[str, float],
    cache: MutableMapping[int, float],
) -> float:
    if result.mask not in cache:
        cache[result.mask] = sum(
            communication_by_sortie.get(sortie_id, 0.0)
            for sortie_id in result.transport_sorties
        )
    return cache[result.mask]


def _balance(values: Sequence[float]) -> float:
    mean_value = sum(values) / len(values)
    if abs(mean_value) <= NUMERIC_TOLERANCE:
        return 0.0
    return (max(values) - min(values)) / mean_value


def _relay_partition_detail(
    k: int,
    partition_id: str,
    results: Sequence[SubsetResourceResult],
    context: ResourceAccountingContext,
) -> Tuple[Dict[str, int], List[Dict[str, object]]]:
    original_rows = {
        str(row.Sortie_ID): row for row in context.relay_table.itertuples(index=False)
    }
    counts: Dict[str, int] = {}
    details: List[Dict[str, object]] = []
    for relay_id in RELAY_IDS:
        per_group: List[str] = []
        for result in results:
            requirement = next(
                (
                    item
                    for item in result.relay_requirements
                    if item.relay_task_id == relay_id
                ),
                None,
            )
            per_group.append(
                "" if requirement is None else _joined(requirement.group_supported_sorties)
            )
        group_count = sum(bool(value) for value in per_group)
        counts[relay_id] = group_count
        source = original_rows[relay_id]
        detail = {
            "K": k,
            "partition_id": partition_id,
            "relay_sortie_id": relay_id,
            "relay_point": str(source.Position_ID),
            "original_supported_sorties": str(source.Covered_Transport_Sorties),
            "groups_involved": group_count,
        }
        for group_index in range(1, 4):
            detail[f"group_{group_index}_supported_sorties"] = (
                per_group[group_index - 1] if group_index <= len(per_group) else ""
            )
        detail["occurrence_count"] = group_count
        detail["extra_occurrence_count"] = max(0, group_count - 1)
        details.append(detail)
    return counts, details


def _group_columns(
    result: SubsetResourceResult,
    group_index: int,
    t_comm: float,
) -> Dict[str, object]:
    prefix = f"group{group_index}_"
    scope = f"Group{group_index}"
    relay_occurrences = tuple(row.relay_task_id for row in result.relay_requirements)
    return {
        prefix + "mask": result.mask,
        prefix + "components": _joined(result.components),
        prefix + "zones": _joined(result.zones),
        prefix + "transport_sorties": _joined(result.transport_sorties),
        prefix + "relay_occurrences": _joined(relay_occurrences),
        prefix + "N_U_A": int(result.transport_uav_requirement.get("A", 0)),
        prefix + "N_U_B": int(result.transport_uav_requirement.get("B", 0)),
        prefix + "N_U_C": int(result.transport_uav_requirement.get("C", 0)),
        prefix + "N_B_A": int(result.battery_requirement.get("A", 0)),
        prefix + "N_B_B": int(result.battery_requirement.get("B", 0)),
        prefix + "N_B_C": int(result.battery_requirement.get("C", 0)),
        prefix + "N_R": int(result.total_relay_uav),
        prefix + "N_E": int(result.total_relay_energy),
        prefix + "Q": int(result.box_count),
        prefix + "M": float(result.total_mass_kg),
        prefix + "V": float(result.total_volume_m3),
        prefix + "N_sortie": int(result.transport_sortie_count),
        prefix + "T_transport": float(result.transport_duration_s),
        prefix + "T_comm": float(t_comm),
        prefix + "transport_uav_assignment": _format_assignment(
            result.transport_uav_assignments, scope
        ),
        prefix + "battery_assignment": _format_assignment(
            result.battery_assignments, scope
        ),
        prefix + "relay_uav_assignment": _format_assignment(
            result.relay_uav_assignments, scope
        ),
        prefix + "relay_energy_assignment": _format_assignment(
            result.relay_energy_assignments, scope
        ),
    }


def evaluate_partition(
    k: int,
    sequence_number: int,
    masks: Sequence[int],
    context: ResourceAccountingContext,
    subset_cache: MutableMapping[int, SubsetResourceResult],
    communication_by_sortie: Mapping[str, float],
    t_comm_cache: MutableMapping[int, float],
) -> Tuple[Dict[str, object], List[Dict[str, object]]]:
    partition_id = f"K{k}-{sequence_number:03d}"
    results = tuple(account_subset(mask, context, subset_cache) for mask in masks)
    t_comm_values = tuple(
        _subset_t_comm(result, communication_by_sortie, t_comm_cache)
        for result in results
    )
    row: Dict[str, object] = {"partition_id": partition_id}
    for group_index, (result, t_comm) in enumerate(
        zip(results, t_comm_values), start=1
    ):
        row.update(_group_columns(result, group_index, t_comm))

    for uav_type in ("A", "B", "C"):
        row[f"R_U_{uav_type}"] = sum(
            int(result.transport_uav_requirement.get(uav_type, 0))
            for result in results
        )
        row[f"R_B_{uav_type}"] = sum(
            int(result.battery_requirement.get(uav_type, 0))
            for result in results
        )
    row["R_U_total"] = sum(row[f"R_U_{uav_type}"] for uav_type in ("A", "B", "C"))
    row["R_B_total"] = sum(row[f"R_B_{uav_type}"] for uav_type in ("A", "B", "C"))
    row["R_R"] = sum(result.total_relay_uav for result in results)
    row["R_E"] = sum(result.total_relay_energy for result in results)

    for uav_type in ("A", "B", "C"):
        row[f"Delta_U_{uav_type}"] = row[f"R_U_{uav_type}"] - BASELINE_UAV[uav_type]
        row[f"Delta_B_{uav_type}"] = (
            row[f"R_B_{uav_type}"] - BASELINE_BATTERY[uav_type]
        )
        row[f"D_U_{uav_type}"] = max(
            0, row[f"R_U_{uav_type}"] - BASELINE_UAV[uav_type]
        )
        row[f"D_B_{uav_type}"] = max(
            0, row[f"R_B_{uav_type}"] - BASELINE_BATTERY[uav_type]
        )
    row["Delta_U_total"] = row["R_U_total"] - sum(BASELINE_UAV.values())
    row["Delta_B_total"] = row["R_B_total"] - sum(BASELINE_BATTERY.values())
    row["Delta_R"] = row["R_R"] - BASELINE_RELAY_UAV
    row["Delta_E"] = row["R_E"] - BASELINE_RELAY_ENERGY
    row["D_R"] = max(0, row["R_R"] - BASELINE_RELAY_UAV)
    row["D_E"] = max(0, row["R_E"] - INVENTORY_RELAY_ENERGY)
    gap_columns = [
        "D_U_A", "D_U_B", "D_U_C", "D_B_A", "D_B_B", "D_B_C", "D_R", "D_E"
    ]
    row["inventory_feasible"] = all(row[column] == 0 for column in gap_columns)

    workload_names = ("Q", "M", "V", "N_sortie", "T_transport", "T_comm")
    for workload_name in workload_names:
        values = [
            float(row[f"group{index}_{workload_name}"])
            for index in range(1, k + 1)
        ]
        row[f"B_{workload_name}"] = _balance(values)

    relay_counts, relay_details = _relay_partition_detail(
        k, partition_id, results, context
    )
    for relay_id in RELAY_IDS:
        row[f"{relay_id}_group_count"] = relay_counts[relay_id]
    row["relay_occurrence_extra_total"] = sum(
        max(0, relay_counts[relay_id] - 1) for relay_id in RELAY_IDS
    )
    return row, relay_details


def pareto_flags(table: pd.DataFrame) -> np.ndarray:
    values = table[PARETO_COLUMNS].to_numpy(dtype=float)
    flags = np.ones(len(values), dtype=bool)
    for index, candidate in enumerate(values):
        no_worse = np.all(values <= candidate + NUMERIC_TOLERANCE, axis=1)
        strictly_better = np.any(values < candidate - NUMERIC_TOLERANCE, axis=1)
        if np.any(no_worse & strictly_better):
            flags[index] = False
    return flags


def independent_pareto_flags(table: pd.DataFrame) -> np.ndarray:
    values = table[PARETO_COLUMNS].to_numpy(dtype=float)
    result = []
    for right_index in range(len(values)):
        dominated = False
        for left_index in range(len(values)):
            if left_index == right_index:
                continue
            left = values[left_index]
            right = values[right_index]
            if np.all(left <= right + NUMERIC_TOLERANCE) and np.any(
                left < right - NUMERIC_TOLERANCE
            ):
                dominated = True
                break
        result.append(not dominated)
    return np.asarray(result, dtype=bool)


def _partition_structure_valid(
    masks: Sequence[int],
    context: ResourceAccountingContext,
) -> Tuple[bool, bool, bool, bool]:
    full_mask = (1 << len(context.phase_data.components)) - 1
    components_ok = (
        sum(bin(mask).count("1") for mask in masks)
        == len(context.phase_data.components)
        and np.bitwise_or.reduce(np.asarray(masks, dtype=int)) == full_mask
        and all(
            left & right == 0
            for index, left in enumerate(masks)
            for right in masks[index + 1 :]
        )
    )
    zone_groups = [
        set(phase123.mask_to_zones(mask, context.phase_data)) for mask in masks
    ]
    all_zones = set().union(*zone_groups)
    zones_ok = (
        all_zones == set(phase123.SERVICE_ZONES)
        and sum(len(group) for group in zone_groups) == len(all_zones)
    )
    sortie_groups = [
        set(phase123.mask_to_transport_sorties(mask, context.phase_data))
        for mask in masks
    ]
    all_sorties = set().union(*sortie_groups)
    expected_sorties = {
        str(record["sortie_id"]) for record in context.phase_data.sortie_records
    }
    sorties_ok = (
        all_sorties == expected_sorties
        and sum(len(group) for group in sortie_groups) == len(all_sorties)
    )
    co_visited_ok = True
    for record in context.phase_data.sortie_records:
        zones = set(record["service_zones"])
        co_visited_ok &= sum(
            bool(zones.intersection(group)) for group in zone_groups
        ) == 1
    return components_ok, zones_ok, sorties_ok, co_visited_ok


def _cross_group_ids_are_isolated(
    masks: Sequence[int],
    subset_cache: Mapping[int, SubsetResourceResult],
) -> bool:
    tables = (
        "transport_uav_assignments",
        "battery_assignments",
        "relay_uav_assignments",
        "relay_energy_assignments",
    )
    for table_name in tables:
        scoped_sets = []
        for group_index, mask in enumerate(masks, start=1):
            table = getattr(subset_cache[mask], table_name)
            scoped_sets.append(
                {
                    f"Group{group_index}::{resource_id}"
                    for resource_id in table["resource_id"]
                }
            )
        if any(
            not scoped_sets[left].isdisjoint(scoped_sets[right])
            for left in range(len(scoped_sets))
            for right in range(left + 1, len(scoped_sets))
        ):
            return False
    return True


def run_exception_checks(
    k2_partitions: Sequence[Tuple[int, ...]],
    k3_partitions: Sequence[Tuple[int, ...]],
    k2_table: pd.DataFrame,
    k3_table: pd.DataFrame,
    subset_cache: Mapping[int, SubsetResourceResult],
    context: ResourceAccountingContext,
) -> pd.DataFrame:
    all_partitions = [*k2_partitions, *k3_partitions]
    structures = [
        _partition_structure_valid(masks, context) for masks in all_partitions
    ]
    all_subset_results = tuple(subset_cache.values())

    reuse = subset_cache[68]
    reuse_uav = reuse.relay_uav_assignments.set_index("task_id")
    reuse_energy = reuse.relay_energy_assignments.set_index("task_id")
    relay_uav_crosspoint = (
        reuse_uav.loc["RS002", "relay_point"]
        != reuse_uav.loc["RS003", "relay_point"]
        and reuse_uav.loc["RS002", "resource_id"]
        == reuse_uav.loc["RS003", "resource_id"]
    )
    relay_energy_crosspoint = (
        reuse_energy.loc["RS002", "relay_point"]
        != reuse_energy.loc["RS003", "relay_point"]
        and reuse_energy.loc["RS002", "resource_id"]
        == reuse_energy.loc["RS003", "resource_id"]
    )
    assignments_nonoverlap = all(
        _no_overlap(table)
        for result in all_subset_results
        for table in (
            result.transport_uav_assignments,
            result.battery_assignments,
            result.relay_uav_assignments,
            result.relay_energy_assignments,
        )
    )
    k2_independent = independent_pareto_flags(k2_table)
    k3_independent = independent_pareto_flags(k3_table)
    checks = [
        ("01_K2_rows_equal_63", len(k2_table) == 63),
        ("02_K3_rows_equal_301", len(k3_table) == 301),
        ("03_each_component_exactly_once", all(row[0] for row in structures)),
        ("04_each_service_zone_exactly_once", all(row[1] for row in structures)),
        ("05_each_transport_sortie_exactly_once", all(row[2] for row in structures)),
        ("06_covisit_zones_never_split", all(row[3] for row in structures)),
        (
            "07_transport_absolute_times_unchanged",
            all(
                _transport_times_fixed(result, context)
                for result in all_subset_results
            ),
        ),
        (
            "08_relay_absolute_times_unchanged",
            all(_relay_times_fixed(result, context) for result in all_subset_results),
        ),
        (
            "09_cross_group_resource_ids_isolated",
            all(
                _cross_group_ids_are_isolated(masks, subset_cache)
                for masks in all_partitions
            ),
        ),
        ("10_same_group_relay_uav_crosspoint_reuse", relay_uav_crosspoint),
        ("11_same_group_relay_energy_crosspoint_reuse", relay_energy_crosspoint),
        ("12_all_resource_assignments_nonoverlap", assignments_nonoverlap),
        (
            "13_gamma_intersection_correct",
            all(
                _gamma_intersections_correct(result)
                for result in all_subset_results
            ),
        ),
        (
            "14_all_pareto_rows_nondominated",
            bool(
                k2_independent[
                    k2_table["is_pareto"].to_numpy(dtype=bool)
                ].all()
            )
            and bool(
                k3_independent[
                    k3_table["is_pareto"].to_numpy(dtype=bool)
                ].all()
            ),
        ),
        (
            "15_is_pareto_matches_independent_check",
            np.array_equal(
                k2_table["is_pareto"].to_numpy(dtype=bool), k2_independent
            )
            and np.array_equal(
                k3_table["is_pareto"].to_numpy(dtype=bool), k3_independent
            ),
        ),
    ]
    return pd.DataFrame(
        {
            "check": [name for name, _ in checks],
            "result": ["PASS" if passed else "FAIL" for _, passed in checks],
        }
    )


def subset_results_table(
    subset_cache: Mapping[int, SubsetResourceResult],
    t_comm_cache: Mapping[int, float],
) -> pd.DataFrame:
    rows = []
    for mask, result in sorted(subset_cache.items()):
        rows.append(
            {
                "mask": mask,
                "components": _joined(result.components),
                "service_zones": _joined(result.zones),
                "transport_sorties": _joined(result.transport_sorties),
                "relay_occurrences": _joined(
                    row.relay_task_id for row in result.relay_requirements
                ),
                "Q": result.box_count,
                "M": result.total_mass_kg,
                "V": result.total_volume_m3,
                "N_sortie": result.transport_sortie_count,
                "T_transport": result.transport_duration_s,
                "T_comm": t_comm_cache[mask],
                "N_U_A": result.transport_uav_requirement.get("A", 0),
                "N_U_B": result.transport_uav_requirement.get("B", 0),
                "N_U_C": result.transport_uav_requirement.get("C", 0),
                "N_B_A": result.battery_requirement.get("A", 0),
                "N_B_B": result.battery_requirement.get("B", 0),
                "N_B_C": result.battery_requirement.get("C", 0),
                "N_R": result.total_relay_uav,
                "N_E": result.total_relay_energy,
                "transport_uav_assignment": _format_assignment(
                    result.transport_uav_assignments
                ),
                "battery_assignment": _format_assignment(
                    result.battery_assignments
                ),
                "relay_uav_assignment": _format_assignment(
                    result.relay_uav_assignments
                ),
                "relay_energy_assignment": _format_assignment(
                    result.relay_energy_assignments
                ),
            }
        )
    return pd.DataFrame(rows)


def pareto_group_details(
    pareto_table: pd.DataFrame,
    k: int,
    subset_cache: Mapping[int, SubsetResourceResult],
    t_comm_cache: Mapping[int, float],
) -> pd.DataFrame:
    rows = []
    for solution in pareto_table.itertuples(index=False):
        for group_index in range(1, k + 1):
            mask = int(getattr(solution, f"group{group_index}_mask"))
            result = subset_cache[mask]
            row = {
                "partition_id": solution.partition_id,
                "group_id": f"Group{group_index}",
            }
            row.update(
                _group_columns(result, group_index, t_comm_cache[mask])
            )
            rows.append(row)
    return pd.DataFrame(rows)


def select_manual_candidates(
    pareto_table: pd.DataFrame,
) -> pd.DataFrame:
    feasible = pareto_table.loc[pareto_table["inventory_feasible"]].copy()
    pool = feasible if not feasible.empty else pareto_table.copy()
    pool["B_max"] = pool[BALANCE_COLUMNS].max(axis=1)
    resource_minima = pool[RESOURCE_COLUMNS].min()
    pool["resource_minimum_dimensions"] = (
        pool[RESOURCE_COLUMNS].eq(resource_minima).sum(axis=1)
    )
    selectors = [
        (
            "minimum relay-UAV requirement",
            ["R_R", "B_max", "partition_id"],
            [True, True, True],
        ),
        (
            "minimum relay-energy requirement",
            ["R_E", "B_max", "partition_id"],
            [True, True, True],
        ),
        (
            "minimum worst six-dimensional imbalance",
            ["B_max", "relay_occurrence_extra_total", "partition_id"],
            [True, True, True],
        ),
        (
            "minimum relay occurrence duplication",
            ["relay_occurrence_extra_total", "B_max", "partition_id"],
            [True, True, True],
        ),
        (
            "covers most resource-dimension minima",
            ["resource_minimum_dimensions", "B_max", "partition_id"],
            [False, True, True],
        ),
    ]
    chosen: Dict[str, List[str]] = defaultdict(list)
    for reason, columns, ascending in selectors:
        selected = pool.sort_values(columns, ascending=ascending).iloc[0]
        chosen[str(selected["partition_id"])].append(reason)
    if len(chosen) < 3:
        for partition_id in pool.sort_values("partition_id")["partition_id"]:
            if partition_id not in chosen:
                chosen[str(partition_id)].append(
                    "additional nondominated comparison case"
                )
            if len(chosen) >= 3:
                break
    chosen_ids = list(chosen)[:5]
    result = pool.loc[pool["partition_id"].isin(chosen_ids)].copy()
    result.insert(
        1,
        "candidate_reason",
        result["partition_id"].map(
            lambda item: "; ".join(chosen[str(item)])
        ),
    )
    result.insert(
        2,
        "candidate_pool",
        "inventory-feasible Pareto" if not feasible.empty else "Pareto",
    )
    return result.sort_values("partition_id").drop(
        columns=["B_max", "resource_minimum_dimensions"]
    )


def _range_text(table: pd.DataFrame, columns: Sequence[str]) -> str:
    return "; ".join(
        f"{column}=[{table[column].min():.6g}, {table[column].max():.6g}]"
        for column in columns
    )


def export_results(
    k2_table: pd.DataFrame,
    k3_table: pd.DataFrame,
    validation: pd.DataFrame,
    relay_summary: pd.DataFrame,
    relay_detail: pd.DataFrame,
    subset_table: pd.DataFrame,
    subset_cache: Mapping[int, SubsetResourceResult],
    t_comm_cache: Mapping[int, float],
    k2_candidates: pd.DataFrame,
    k3_candidates: pd.DataFrame,
) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    k2_pareto = k2_table.loc[k2_table["is_pareto"]].copy()
    k3_pareto = k3_table.loc[k3_table["is_pareto"]].copy()
    with pd.ExcelWriter(K2_ALL_OUTPUT, engine="openpyxl") as writer:
        k2_table.to_excel(writer, sheet_name="K2_all", index=False)
        validation.to_excel(writer, sheet_name="validation_15", index=False)
    with pd.ExcelWriter(K3_ALL_OUTPUT, engine="openpyxl") as writer:
        k3_table.to_excel(writer, sheet_name="K3_all", index=False)
        validation.to_excel(writer, sheet_name="validation_15", index=False)
    with pd.ExcelWriter(K2_PARETO_OUTPUT, engine="openpyxl") as writer:
        k2_pareto.to_excel(writer, sheet_name="Pareto_K2", index=False)
        pareto_group_details(
            k2_pareto, 2, subset_cache, t_comm_cache
        ).to_excel(writer, sheet_name="group_details", index=False)
    with pd.ExcelWriter(K3_PARETO_OUTPUT, engine="openpyxl") as writer:
        k3_pareto.to_excel(writer, sheet_name="Pareto_K3", index=False)
        pareto_group_details(
            k3_pareto, 3, subset_cache, t_comm_cache
        ).to_excel(writer, sheet_name="group_details", index=False)
    with pd.ExcelWriter(SUBSET_OUTPUT, engine="openpyxl") as writer:
        subset_table.to_excel(writer, sheet_name="subset_results", index=False)
    with pd.ExcelWriter(RELAY_DUPLICATION_OUTPUT, engine="openpyxl") as writer:
        relay_summary.to_excel(
            writer, sheet_name="partition_summary", index=False
        )
        relay_detail.to_excel(writer, sheet_name="relay_detail", index=False)
    with pd.ExcelWriter(CANDIDATE_OUTPUT, engine="openpyxl") as writer:
        k2_candidates.to_excel(writer, sheet_name="K2_candidates", index=False)
        k3_candidates.to_excel(writer, sheet_name="K3_candidates", index=False)


def main() -> None:
    started = perf_counter()
    for key in INTERVAL_PARTITION_CALLS:
        INTERVAL_PARTITION_CALLS[key] = 0
    context = load_context()
    m = len(context.phase_data.components)
    k2_partitions = tuple(phase123.enumerate_k2_partitions(m))
    k3_partitions = tuple(phase123.enumerate_k3_partitions(m))
    subset_cache: Dict[int, SubsetResourceResult] = {}
    t_comm_cache: Dict[int, float] = {}
    communication_by_sortie = _communication_duration_by_sortie(context)

    all_rows: Dict[int, List[Dict[str, object]]] = {2: [], 3: []}
    relay_details: List[Dict[str, object]] = []
    for k, partitions in ((2, k2_partitions), (3, k3_partitions)):
        for sequence_number, masks in enumerate(partitions, start=1):
            row, detail = evaluate_partition(
                k,
                sequence_number,
                masks,
                context,
                subset_cache,
                communication_by_sortie,
                t_comm_cache,
            )
            all_rows[k].append(row)
            relay_details.extend(detail)

    k2_table = pd.DataFrame(all_rows[2])
    k3_table = pd.DataFrame(all_rows[3])
    k2_table["is_pareto"] = pareto_flags(k2_table)
    k3_table["is_pareto"] = pareto_flags(k3_table)
    relay_detail = pd.DataFrame(relay_details)
    relay_summary_columns = [
        "partition_id",
        "RS001_group_count",
        "RS002_group_count",
        "RS003_group_count",
        "relay_occurrence_extra_total",
        "R_R",
        "Delta_R",
        "R_E",
        "Delta_E",
    ]
    relay_summary = pd.concat(
        [
            k2_table[relay_summary_columns].assign(K=2),
            k3_table[relay_summary_columns].assign(K=3),
        ],
        ignore_index=True,
    )
    relay_summary = relay_summary[["K", *relay_summary_columns]]

    validation = run_exception_checks(
        k2_partitions,
        k3_partitions,
        k2_table,
        k3_table,
        subset_cache,
        context,
    )
    subset_table = subset_results_table(subset_cache, t_comm_cache)
    k2_pareto = k2_table.loc[k2_table["is_pareto"]]
    k3_pareto = k3_table.loc[k3_table["is_pareto"]]
    k2_candidates = select_manual_candidates(k2_pareto)
    k3_candidates = select_manual_candidates(k3_pareto)
    export_results(
        k2_table,
        k3_table,
        validation,
        relay_summary,
        relay_detail,
        subset_table,
        subset_cache,
        t_comm_cache,
        k2_candidates,
        k3_candidates,
    )

    negative_delta_columns = [
        "Delta_U_A",
        "Delta_U_B",
        "Delta_U_C",
        "Delta_U_total",
        "Delta_B_A",
        "Delta_B_B",
        "Delta_B_C",
        "Delta_B_total",
        "Delta_R",
        "Delta_E",
    ]
    negative_delta_count = sum(
        int((table[negative_delta_columns] < 0).any(axis=1).sum())
        for table in (k2_table, k3_table)
    )
    runtime_s = perf_counter() - started
    log_lines = [
        "",
        "=" * 72,
        "Problem 4 exact K=2/K=3 enumeration",
        "=" * 72,
        f"m = {m}",
        f"K2 theoretical = {2 ** (m - 1) - 1}",
        f"K2 actual = {len(k2_table)}",
        f"K3 theoretical = {phase123.stirling_second_kind_m3(m)}",
        f"K3 actual = {len(k3_table)}",
        f"unique masks solved = {len(subset_cache)}",
        f"transport interval calls = {INTERVAL_PARTITION_CALLS['transport_uav']}",
        f"battery interval calls = {INTERVAL_PARTITION_CALLS['battery']}",
        f"relay UAV interval calls = {INTERVAL_PARTITION_CALLS['relay_uav']}",
        f"relay energy interval calls = {INTERVAL_PARTITION_CALLS['relay_energy']}",
        "Gurobi calls = 0",
        f"K2 Pareto count = {len(k2_pareto)}",
        f"K3 Pareto count = {len(k3_pareto)}",
        f"K2 inventory feasible count = {int(k2_table['inventory_feasible'].sum())}",
        f"K3 inventory feasible count = {int(k3_table['inventory_feasible'].sum())}",
        f"K2 resource ranges: {_range_text(k2_table, RESOURCE_COLUMNS)}",
        f"K3 resource ranges: {_range_text(k3_table, RESOURCE_COLUMNS)}",
        f"K2 balance ranges: {_range_text(k2_table, BALANCE_COLUMNS)}",
        f"K3 balance ranges: {_range_text(k3_table, BALANCE_COLUMNS)}",
        f"K2 relay duplication range = [{k2_table['relay_occurrence_extra_total'].min()}, {k2_table['relay_occurrence_extra_total'].max()}]",
        f"K3 relay duplication range = [{k3_table['relay_occurrence_extra_total'].min()}, {k3_table['relay_occurrence_extra_total'].max()}]",
        f"partitions containing a negative Delta = {negative_delta_count}",
        "T_comm definition = sum of all Problem-3 communication-demand intervals (direct + relay)",
        "Because Problem 4 inherits fixed Problem-3 task times, all four resource classes use exact fixed-interval partitioning; Gurobi is not used.",
    ]
    log_lines.extend(
        f"[{row.result}] {row.check}"
        for row in validation.itertuples(index=False)
    )
    log_lines.extend(
        [
            f"K2 manual-comparison candidates = {_joined(k2_candidates['partition_id'])}",
            f"K3 manual-comparison candidates = {_joined(k3_candidates['partition_id'])}",
            f"total runtime s = {runtime_s:.6f}",
            f"outputs = {K2_ALL_OUTPUT}; {K3_ALL_OUTPUT}; {K2_PARETO_OUTPUT}; {K3_PARETO_OUTPUT}; {SUBSET_OUTPUT}; {RELAY_DUPLICATION_OUTPUT}; {CANDIDATE_OUTPUT}",
        ]
    )
    message = "\n".join(log_lines)
    print(message)
    with RUN_LOG.open("a", encoding="utf-8") as stream:
        stream.write(message + "\n")


if __name__ == "__main__":
    main()
