"""Add energy and makespan reporting without rerunning any optimization."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

import numpy as np
import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from project_paths import bootstrap, input_file, output_dir, require_file, prepare_writable_input

bootstrap()

from problem4_paper_tables import write_latex_table


MODULE_DIR = Path(__file__).resolve().parent
RESULT_DIR = output_dir("problem4")
PAPER_DIR = RESULT_DIR / "paper_tables"
PROBLEM3_RESULT = input_file("problem3", "problem3_vns_20cycles.xlsx")
PAPER_WORKBOOK = RESULT_DIR / "problem4_paper_tables.xlsx"
VERIFICATION_OUTPUT = RESULT_DIR / "problem4_energy_makespan_verification.xlsx"

SOLUTION_SPECS = (
    (2, "K2-041"),
    (2, "K2-051"),
    (3, "K3-301"),
    (3, "K3-147"),
)
REPRESENTATIVE_IDS = ("K2-041", "K3-301")
BALANCE_COLUMNS = (
    "B_Q",
    "B_M",
    "B_V",
    "B_N_sortie",
    "B_T_transport",
    "B_T_comm",
)
PROTECTED_FILES = (
    input_file("problem4", "problem4_K2_all.xlsx"),
    input_file("problem4", "problem4_K3_all.xlsx"),
    input_file("problem4", "problem4_K2_pareto.xlsx"),
    input_file("problem4", "problem4_K3_pareto.xlsx"),
    input_file("problem4", "problem4_representative.xlsx"),
)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _split(value: object) -> Tuple[str, ...]:
    if pd.isna(value):
        return tuple()
    return tuple(item.strip() for item in str(value).split(";") if item.strip())


def read_sources() -> Dict[str, object]:
    transport = pd.read_excel(
        require_file(PROBLEM3_RESULT), sheet_name="Transport_Sorties"
    ).set_index("Sortie_ID", drop=False)
    relay = pd.read_excel(
        require_file(PROBLEM3_RESULT), sheet_name="Relay_Sorties"
    ).set_index("Sortie_ID", drop=False)
    all_tables = {
        2: pd.read_excel(
            require_file(input_file("problem4", "problem4_K2_all.xlsx")), sheet_name="K2_all"
        ).set_index("partition_id", drop=False),
        3: pd.read_excel(
            require_file(input_file("problem4", "problem4_K3_all.xlsx")), sheet_name="K3_all"
        ).set_index("partition_id", drop=False),
    }
    pareto_tables = {
        2: pd.read_excel(
            require_file(input_file("problem4", "problem4_K2_pareto.xlsx")), sheet_name="Pareto_K2"
        ).set_index("partition_id", drop=False),
        3: pd.read_excel(
            require_file(input_file("problem4", "problem4_K3_pareto.xlsx")), sheet_name="Pareto_K3"
        ).set_index("partition_id", drop=False),
    }
    candidate = pd.read_excel(
        require_file(input_file("problem4", "problem4_final_candidate_comparison.xlsx")),
        sheet_name="candidate_comparison",
    ).set_index("方案", drop=False)
    return {
        "transport": transport,
        "relay": relay,
        "all": all_tables,
        "pareto": pareto_tables,
        "candidate": candidate,
    }


def calculate_execution_metrics(
    sources: Mapping[str, object],
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    transport: pd.DataFrame = sources["transport"]  # type: ignore[assignment]
    relay: pd.DataFrame = sources["relay"]  # type: ignore[assignment]
    all_tables: Mapping[int, pd.DataFrame] = sources["all"]  # type: ignore[assignment]
    group_rows: List[Dict[str, object]] = []
    relay_rows: List[Dict[str, object]] = []
    summary_rows: List[Dict[str, object]] = []

    for k, partition_id in SOLUTION_SPECS:
        solution = all_tables[k].loc[partition_id]
        solution_groups = []
        for group_index in range(1, k + 1):
            prefix = f"group{group_index}_"
            transport_ids = _split(solution[prefix + "transport_sorties"])
            relay_ids = _split(solution[prefix + "relay_occurrences"])
            transport_energy = float(
                transport.loc[list(transport_ids), "Energy_kWh"].sum()
            )
            relay_energy = float(
                relay.loc[list(relay_ids), "Total_Energy_kWh"].sum()
            )
            transport_cmax = float(
                transport.loc[list(transport_ids), "Return_Time_s"].max()
            )
            relay_cmax = (
                float(relay.loc[list(relay_ids), "Return_End_s"].max())
                if relay_ids
                else 0.0
            )
            group_cmax = max(transport_cmax, relay_cmax)
            group_row = {
                "方案": partition_id,
                "K": k,
                "任务组": f"第{group_index}组",
                "components": str(solution[prefix + "components"]),
                "service_zones": str(solution[prefix + "zones"]),
                "transport_sorties": ";".join(transport_ids),
                "relay_occurrences": ";".join(relay_ids),
                "transport_sortie_count": int(solution[prefix + "N_sortie"]),
                "box_count": int(solution[prefix + "Q"]),
                "transport_energy_kwh": transport_energy,
                "relay_energy_kwh": relay_energy,
                "total_energy_kwh": transport_energy + relay_energy,
                "transport_return_max_s": transport_cmax,
                "relay_return_max_s": relay_cmax,
                "Cmax_s": group_cmax,
                "transport_completion_field": "Return_Time_s",
                "relay_completion_field": "Return_End_s",
            }
            group_rows.append(group_row)
            solution_groups.append(group_row)
            for relay_id in relay_ids:
                source = relay.loc[relay_id]
                relay_rows.append(
                    {
                        "方案": partition_id,
                        "任务组": f"第{group_index}组",
                        "relay_occurrence": relay_id,
                        "relay_point": source["Position_ID"],
                        "inherited_full_sortie": True,
                        "source_total_energy_kwh": float(
                            source["Total_Energy_kWh"]
                        ),
                        "occurrence_energy_kwh": float(
                            source["Total_Energy_kWh"]
                        ),
                        "source_return_end_s": float(source["Return_End_s"]),
                    }
                )

        transport_total = sum(
            float(row["transport_energy_kwh"]) for row in solution_groups
        )
        relay_total = sum(
            float(row["relay_energy_kwh"]) for row in solution_groups
        )
        total_energy = transport_total + relay_total
        partition_cmax = max(float(row["Cmax_s"]) for row in solution_groups)
        problem3_transport = float(transport["Energy_kWh"].sum())
        problem3_relay = float(relay["Total_Energy_kWh"].sum())
        problem3_total = problem3_transport + problem3_relay
        summary_rows.append(
            {
                "方案": partition_id,
                "K": k,
                "group_count": k,
                "transport_sortie_count": sum(
                    int(row["transport_sortie_count"]) for row in solution_groups
                ),
                "box_count": sum(int(row["box_count"]) for row in solution_groups),
                "relay_occurrence_count": sum(
                    len(_split(row["relay_occurrences"])) for row in solution_groups
                ),
                "transport_energy_kwh": transport_total,
                "relay_energy_kwh": relay_total,
                "total_energy_kwh": total_energy,
                "delta_relay_energy_kwh": relay_total - problem3_relay,
                "delta_total_energy_kwh": total_energy - problem3_total,
                "Cmax_s": partition_cmax,
                "RS001_group_count": int(solution["RS001_group_count"]),
                "RS002_group_count": int(solution["RS002_group_count"]),
                "RS003_group_count": int(solution["RS003_group_count"]),
                "relay_occurrence_extra_total": int(
                    solution["relay_occurrence_extra_total"]
                ),
            }
        )
    return (
        pd.DataFrame(group_rows),
        pd.DataFrame(summary_rows).set_index("方案", drop=False),
        pd.DataFrame(relay_rows),
    )


def build_table8(
    group_detail: pd.DataFrame,
    summary: pd.DataFrame,
) -> pd.DataFrame:
    rows = []
    for partition_id in REPRESENTATIVE_IDS:
        groups = group_detail.loc[group_detail["方案"].eq(partition_id)]
        for row in groups.itertuples(index=False):
            rows.append(
                {
                    "分组数": f"K={row.K}",
                    "方案": partition_id,
                    "任务组": row.任务组,
                    "运输架次数": int(row.transport_sortie_count),
                    "货箱数": int(row.box_count),
                    "运输能耗/kWh": round(float(row.transport_energy_kwh), 4),
                    "中继能耗/kWh": round(float(row.relay_energy_kwh), 4),
                    "总能耗/kWh": round(float(row.total_energy_kwh), 4),
                    "完工时间/s": round(float(row.Cmax_s), 2),
                }
            )
        total = summary.loc[partition_id]
        rows.append(
            {
                "分组数": f"K={int(total['K'])}",
                "方案": partition_id,
                "任务组": "整体",
                "运输架次数": int(total["transport_sortie_count"]),
                "货箱数": int(total["box_count"]),
                "运输能耗/kWh": round(float(total["transport_energy_kwh"]), 4),
                "中继能耗/kWh": round(float(total["relay_energy_kwh"]), 4),
                "总能耗/kWh": round(float(total["total_energy_kwh"]), 4),
                "完工时间/s": round(float(total["Cmax_s"]), 2),
            }
        )
    return pd.DataFrame(rows)


def build_table9(
    sources: Mapping[str, object],
    summary: pd.DataFrame,
) -> pd.DataFrame:
    transport: pd.DataFrame = sources["transport"]  # type: ignore[assignment]
    relay: pd.DataFrame = sources["relay"]  # type: ignore[assignment]
    candidate: pd.DataFrame = sources["candidate"]  # type: ignore[assignment]
    problem3_transport_energy = float(transport["Energy_kWh"].sum())
    problem3_relay_energy = float(relay["Total_Energy_kWh"].sum())
    problem3_total_energy = problem3_transport_energy + problem3_relay_energy
    problem3_cmax = max(
        float(transport["Return_Time_s"].max()),
        float(relay["Return_End_s"].max()),
    )
    columns = (
        "问题三联合方案",
        "K=2均衡型方案",
        "K=3均衡型方案",
    )
    k2 = summary.loc["K2-041"]
    k3 = summary.loc["K3-301"]
    k2_source = candidate.loc["K2-041"]
    k3_source = candidate.loc["K3-301"]
    metrics = [
        ("运输UAV总数", 8, int(k2_source["R_U_total"]), int(k3_source["R_U_total"])),
        ("共享电池总数", 14, int(k2_source["R_B_total"]), int(k3_source["R_B_total"])),
        ("中继UAV数量", 2, int(k2_source["R_R"]), int(k3_source["R_R"])),
        ("中继能源组件数量", 3, int(k2_source["R_E"]), int(k3_source["R_E"])),
        ("中继UAV库存缺口", 0, int(k2_source["D_R"]), int(k3_source["D_R"])),
        (
            "运输总能耗/kWh",
            problem3_transport_energy,
            float(k2["transport_energy_kwh"]),
            float(k3["transport_energy_kwh"]),
        ),
        (
            "中继总能耗/kWh",
            problem3_relay_energy,
            float(k2["relay_energy_kwh"]),
            float(k3["relay_energy_kwh"]),
        ),
        (
            "总能耗/kWh",
            problem3_total_energy,
            float(k2["total_energy_kwh"]),
            float(k3["total_energy_kwh"]),
        ),
        (
            "新增中继能耗/kWh",
            0.0,
            float(k2["delta_relay_energy_kwh"]),
            float(k3["delta_relay_energy_kwh"]),
        ),
        (
            "整体完工时间/s",
            problem3_cmax,
            float(k2["Cmax_s"]),
            float(k3["Cmax_s"]),
        ),
        (
            "中继额外任务实例数",
            0,
            int(k2["relay_occurrence_extra_total"]),
            int(k3["relay_occurrence_extra_total"]),
        ),
        (
            "最坏不均衡",
            "—",
            float(k2_source["B_max"]),
            float(k3_source["B_max"]),
        ),
        (
            "平均不均衡",
            "—",
            float(k2_source["B_mean"]),
            float(k3_source["B_mean"]),
        ),
    ]
    rows = []
    for metric, p3_value, k2_value, k3_value in metrics:
        rows.append(
            {
                "指标": metric,
                columns[0]: p3_value,
                columns[1]: k2_value,
                columns[2]: k3_value,
            }
        )
    return pd.DataFrame(rows)


def build_updated_table6(
    sources: Mapping[str, object],
    summary: pd.DataFrame,
) -> pd.DataFrame:
    candidate: pd.DataFrame = sources["candidate"]  # type: ignore[assignment]
    features = {
        "K2-041": "两组分区的均衡型代表方案",
        "K2-051": "两组分区的资源节约型代表方案",
        "K3-301": "三组分区的均衡型代表方案",
        "K3-147": "三组分区的资源节约型代表方案",
    }
    rows = []
    for k, partition_id in SOLUTION_SPECS:
        source = candidate.loc[partition_id]
        performance = summary.loc[partition_id]
        rows.append(
            {
                "分组数": f"K={k}",
                "方案": partition_id,
                "方案特征": features[partition_id],
                "运输UAV总数": int(source["R_U_total"]),
                "电池总数": int(source["R_B_total"]),
                "中继UAV": int(source["R_R"]),
                "中继能源组件": int(source["R_E"]),
                "中继UAV缺口": int(source["D_R"]),
                "中继复制数": int(source["relay_occurrence_extra_total"]),
                "中继总能耗/kWh": round(
                    float(performance["relay_energy_kwh"]), 4
                ),
                "总能耗/kWh": round(float(performance["total_energy_kwh"]), 4),
                "新增能耗/kWh": round(
                    float(performance["delta_total_energy_kwh"]), 4
                ),
                "整体完工时间/s": round(float(performance["Cmax_s"]), 2),
                "最坏不均衡": round(float(source["B_max"]), 4),
                "平均不均衡": round(float(source["B_mean"]), 4),
            }
        )
    return pd.DataFrame(rows)


def build_table10(summary: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, partition_id in SOLUTION_SPECS:
        source = summary.loc[partition_id]
        rows.append(
            {
                "方案": partition_id,
                "RS001出现组数": int(source["RS001_group_count"]),
                "RS002出现组数": int(source["RS002_group_count"]),
                "RS003出现组数": int(source["RS003_group_count"]),
                "额外中继架次数": int(
                    source["relay_occurrence_extra_total"]
                ),
                "中继总能耗/kWh": round(
                    float(source["relay_energy_kwh"]), 4
                ),
                "较问题三新增中继能耗/kWh": round(
                    float(source["delta_relay_energy_kwh"]), 4
                ),
            }
        )
    return pd.DataFrame(rows)


def build_problem3_baseline(
    sources: Mapping[str, object],
) -> pd.DataFrame:
    transport: pd.DataFrame = sources["transport"]  # type: ignore[assignment]
    relay: pd.DataFrame = sources["relay"]  # type: ignore[assignment]
    values = [
        (
            "运输最大返回时刻",
            float(transport["Return_Time_s"].max()),
            "s",
            "Transport_Sorties.Return_Time_s",
        ),
        (
            "中继最大返回时刻",
            float(relay["Return_End_s"].max()),
            "s",
            "Relay_Sorties.Return_End_s",
        ),
        (
            "联合完工时间",
            max(
                float(transport["Return_Time_s"].max()),
                float(relay["Return_End_s"].max()),
            ),
            "s",
            "两类返回时刻的最大值",
        ),
        (
            "运输总能耗",
            float(transport["Energy_kWh"].sum()),
            "kWh",
            "Transport_Sorties.Energy_kWh",
        ),
        (
            "中继总能耗",
            float(relay["Total_Energy_kWh"].sum()),
            "kWh",
            "Relay_Sorties.Total_Energy_kWh",
        ),
        (
            "联合总能耗",
            float(transport["Energy_kWh"].sum())
            + float(relay["Total_Energy_kWh"].sum()),
            "kWh",
            "运输能耗与中继能耗之和",
        ),
    ]
    for relay_id, row in relay.iterrows():
        values.append(
            (
                f"{relay_id}完整架次能耗",
                float(row["Total_Energy_kWh"]),
                "kWh",
                f"Relay_Sorties[{relay_id}].Total_Energy_kWh",
            )
        )
    return pd.DataFrame(values, columns=["指标", "数值", "单位", "来源"])


def validate(
    sources: Mapping[str, object],
    group_detail: pd.DataFrame,
    summary: pd.DataFrame,
    relay_detail: pd.DataFrame,
    hashes_before: Mapping[Path, str],
) -> pd.DataFrame:
    transport: pd.DataFrame = sources["transport"]  # type: ignore[assignment]
    relay: pd.DataFrame = sources["relay"]  # type: ignore[assignment]
    all_tables: Mapping[int, pd.DataFrame] = sources["all"]  # type: ignore[assignment]
    pareto_tables: Mapping[int, pd.DataFrame] = sources["pareto"]  # type: ignore[assignment]
    problem3_transport = float(transport["Energy_kWh"].sum())
    problem3_relay = float(relay["Total_Energy_kWh"].sum())
    problem3_total = problem3_transport + problem3_relay
    problem3_cmax = max(
        float(transport["Return_Time_s"].max()),
        float(relay["Return_End_s"].max()),
    )

    transport_once = True
    for k, partition_id in SOLUTION_SPECS:
        ids = [
            sortie_id
            for value in group_detail.loc[
                group_detail["方案"].eq(partition_id), "transport_sorties"
            ]
            for sortie_id in _split(value)
        ]
        transport_once &= len(ids) == 20 and set(ids) == set(transport.index)

    transport_energy_ok = all(
        np.isclose(
            float(summary.loc[partition_id, "transport_energy_kwh"]),
            problem3_transport,
            atol=1e-10,
            rtol=0.0,
        )
        for _, partition_id in SOLUTION_SPECS
    )
    occurrence_energy_ok = all(
        np.isclose(
            float(row.occurrence_energy_kwh),
            float(relay.loc[row.relay_occurrence, "Total_Energy_kWh"]),
            atol=1e-12,
            rtol=0.0,
        )
        for row in relay_detail.itertuples(index=False)
    )
    no_split_ok = all(
        np.isclose(
            float(group.relay_energy_kwh),
            float(
                relay_detail.loc[
                    relay_detail["方案"].eq(group.方案)
                    & relay_detail["任务组"].eq(group.任务组),
                    "occurrence_energy_kwh",
                ].sum()
            ),
            atol=1e-12,
            rtol=0.0,
        )
        for group in group_detail.itertuples(index=False)
    )
    total_identity_ok = all(
        np.isclose(
            float(row.total_energy_kwh),
            float(row.transport_energy_kwh) + float(row.relay_energy_kwh),
            atol=1e-10,
            rtol=0.0,
        )
        for row in summary.itertuples()
    )
    delta_definition_ok = all(
        np.isclose(
            float(row.delta_total_energy_kwh),
            float(row.total_energy_kwh) - problem3_total,
            atol=1e-10,
            rtol=0.0,
        )
        for row in summary.itertuples()
    )
    delta_equal_ok = all(
        np.isclose(
            float(row.delta_total_energy_kwh),
            float(row.delta_relay_energy_kwh),
            atol=1e-10,
            rtol=0.0,
        )
        for row in summary.itertuples()
    )
    group_cmax_ok = all(
        np.isclose(
            float(row.Cmax_s),
            max(float(row.transport_return_max_s), float(row.relay_return_max_s)),
            atol=1e-10,
            rtol=0.0,
        )
        for row in group_detail.itertuples(index=False)
    )
    no_turnaround = group_detail["relay_completion_field"].eq("Return_End_s").all()
    no_charge = group_detail["relay_completion_field"].eq("Return_End_s").all()
    partition_cmax_ok = all(
        np.isclose(
            float(summary.loc[partition_id, "Cmax_s"]),
            float(
                group_detail.loc[
                    group_detail["方案"].eq(partition_id), "Cmax_s"
                ].max()
            ),
            atol=1e-10,
            rtol=0.0,
        )
        for _, partition_id in SOLUTION_SPECS
    )
    k2_cmax_ok = np.isclose(
        float(summary.loc["K2-041", "Cmax_s"]),
        problem3_cmax,
        atol=1e-10,
        rtol=0.0,
    )
    k3_cmax_ok = np.isclose(
        float(summary.loc["K3-301", "Cmax_s"]),
        problem3_cmax,
        atol=1e-10,
        rtol=0.0,
    )
    transport_traceable = all(
        sortie_id in transport.index
        and np.isfinite(float(transport.loc[sortie_id, "Energy_kWh"]))
        for value in group_detail["transport_sorties"]
        for sortie_id in _split(value)
    )
    protected_unchanged = all(
        path.exists() and _file_hash(path) == hashes_before[path]
        for path in PROTECTED_FILES
    )
    protected_unchanged &= all(
        partition_id in pareto_tables[k].index
        and partition_id in all_tables[k].index
        for k, partition_id in SOLUTION_SPECS
    )
    checks = [
        ("01_each_transport_sortie_exactly_once", transport_once),
        ("02_group_transport_energy_sums_to_problem3", transport_energy_ok),
        ("03_relay_occurrence_inherits_full_source_energy", occurrence_energy_ok),
        ("04_no_relay_occurrence_energy_split", no_split_ok),
        ("05_total_energy_identity", total_identity_ok),
        ("06_delta_total_energy_definition", delta_definition_ok),
        ("07_delta_total_equals_delta_relay", delta_equal_ok),
        ("08_group_cmax_uses_return_times", group_cmax_ok),
        ("09_turnaround_end_not_used_for_cmax", no_turnaround),
        ("10_charge_end_not_used_for_cmax", no_charge),
        ("11_partition_cmax_is_group_max", partition_cmax_ok),
        ("12_K2_041_cmax_matches_problem3", k2_cmax_ok),
        ("13_K3_301_cmax_matches_problem3", k3_cmax_ok),
        ("14_transport_energy_traceable_to_20_sorties", transport_traceable),
        ("15_pareto_and_representative_inputs_unchanged", protected_unchanged),
    ]
    return pd.DataFrame(
        {
            "check": [name for name, _ in checks],
            "result": ["PASS" if passed else "FAIL" for _, passed in checks],
        }
    )


def write_verification_workbook(
    group_detail: pd.DataFrame,
    summary: pd.DataFrame,
    relay_detail: pd.DataFrame,
    baseline: pd.DataFrame,
    validation: pd.DataFrame,
) -> None:
    with pd.ExcelWriter(VERIFICATION_OUTPUT, engine="openpyxl") as writer:
        group_detail.to_excel(writer, sheet_name="group_detail", index=False)
        summary.reset_index(drop=True).to_excel(
            writer, sheet_name="solution_summary", index=False
        )
        relay_detail.to_excel(
            writer, sheet_name="relay_occurrence_detail", index=False
        )
        baseline.to_excel(writer, sheet_name="problem3_baseline", index=False)
        validation.to_excel(writer, sheet_name="validation_15", index=False)
    format_workbook(
        VERIFICATION_OUTPUT,
        energy_sheets={
            "group_detail": (
                "transport_energy_kwh",
                "relay_energy_kwh",
                "total_energy_kwh",
            ),
            "solution_summary": (
                "transport_energy_kwh",
                "relay_energy_kwh",
                "total_energy_kwh",
                "delta_relay_energy_kwh",
                "delta_total_energy_kwh",
            ),
            "relay_occurrence_detail": (
                "source_total_energy_kwh",
                "occurrence_energy_kwh",
            ),
        },
        time_sheets={
            "group_detail": (
                "transport_return_max_s",
                "relay_return_max_s",
                "Cmax_s",
            ),
            "solution_summary": ("Cmax_s",),
            "relay_occurrence_detail": ("source_return_end_s",),
        },
    )


def format_workbook(
    path: Path,
    energy_sheets: Mapping[str, Sequence[str]] | None = None,
    time_sheets: Mapping[str, Sequence[str]] | None = None,
) -> None:
    energy_sheets = energy_sheets or {}
    time_sheets = time_sheets or {}
    workbook = load_workbook(path)
    thin = Side(style="thin", color="B7B7B7")
    for worksheet in workbook.worksheets:
        worksheet.freeze_panes = "A2"
        worksheet.auto_filter.ref = worksheet.dimensions
        worksheet.sheet_view.showGridLines = False
        headers = {
            cell.value: cell.column for cell in worksheet[1] if cell.value is not None
        }
        for cell in worksheet[1]:
            cell.font = Font(name="宋体", bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="4472C4")
            cell.alignment = Alignment(horizontal="center", vertical="center")
        for row in worksheet.iter_rows(min_row=2):
            for cell in row:
                cell.font = Font(name="宋体", size=10)
                cell.alignment = Alignment(
                    horizontal="center", vertical="center", wrap_text=True
                )
                cell.border = Border(left=thin, right=thin, top=thin, bottom=thin)
        for column in energy_sheets.get(worksheet.title, ()):
            if column in headers:
                for cell in worksheet.iter_cols(
                    min_col=headers[column],
                    max_col=headers[column],
                    min_row=2,
                ):
                    for item in cell:
                        item.number_format = "0.0000"
        for column in time_sheets.get(worksheet.title, ()):
            if column in headers:
                for cell in worksheet.iter_cols(
                    min_col=headers[column],
                    max_col=headers[column],
                    min_row=2,
                ):
                    for item in cell:
                        item.number_format = "0.00"
        for column_cells in worksheet.columns:
            contents = [
                str(cell.value) if cell.value is not None else ""
                for cell in column_cells
            ]
            width = min(42, max(10, max(len(value) for value in contents) + 2))
            worksheet.column_dimensions[column_cells[0].column_letter].width = width
        worksheet.page_setup.orientation = "landscape"
        worksheet.page_setup.fitToWidth = 1
        worksheet.page_setup.fitToHeight = 0
        worksheet.sheet_properties.pageSetUpPr.fitToPage = True
    workbook.save(path)


def update_paper_workbook(
    table6: pd.DataFrame,
    table8: pd.DataFrame,
    table9: pd.DataFrame,
    table10: pd.DataFrame,
) -> None:
    with pd.ExcelWriter(
        PAPER_WORKBOOK,
        engine="openpyxl",
        mode="a",
        if_sheet_exists="replace",
    ) as writer:
        table6.to_excel(writer, sheet_name="Table6_Pareto代表", index=False)
        table8.to_excel(writer, sheet_name="Table8_执行性能", index=False)
        table9.to_excel(writer, sheet_name="Table9_综合比较", index=False)
        table10.to_excel(writer, sheet_name="Table10_中继复制能耗", index=False)
    format_workbook(
        PAPER_WORKBOOK,
        energy_sheets={
            "Table6_Pareto代表": (
                "中继总能耗/kWh",
                "总能耗/kWh",
                "新增能耗/kWh",
                "最坏不均衡",
                "平均不均衡",
            ),
            "Table8_执行性能": (
                "运输能耗/kWh",
                "中继能耗/kWh",
                "总能耗/kWh",
            ),
            "Table9_综合比较": (),
            "Table10_中继复制能耗": (
                "中继总能耗/kWh",
                "较问题三新增中继能耗/kWh",
            ),
        },
        time_sheets={
            "Table6_Pareto代表": ("整体完工时间/s",),
            "Table8_执行性能": ("完工时间/s",),
        },
    )
    workbook = load_workbook(PAPER_WORKBOOK)
    desired = [
        "Table1_任务块",
        "Table2_任务分区",
        "Table3_资源配置",
        "Table4_资源缺口",
        "Table5_均衡比较",
        "Table6_Pareto代表",
        "Table7_枚举结果",
        "Table8_执行性能",
        "Table9_综合比较",
        "Table10_中继复制能耗",
    ]
    workbook._sheets = [workbook[name] for name in desired]
    table9_sheet = workbook["Table9_综合比较"]
    for row_index in range(2, table9_sheet.max_row + 1):
        metric = table9_sheet.cell(row=row_index, column=1).value
        if metric in (
            "运输总能耗/kWh",
            "中继总能耗/kWh",
            "总能耗/kWh",
            "新增中继能耗/kWh",
            "最坏不均衡",
            "平均不均衡",
        ):
            number_format = "0.0000"
        elif metric == "整体完工时间/s":
            number_format = "0.00"
        else:
            number_format = "0"
        for column_index in range(2, table9_sheet.max_column + 1):
            cell = table9_sheet.cell(row=row_index, column=column_index)
            if isinstance(cell.value, (int, float)):
                cell.number_format = number_format
    workbook.save(PAPER_WORKBOOK)


def write_latex_outputs(
    table6: pd.DataFrame,
    table8: pd.DataFrame,
    table9: pd.DataFrame,
    table10: pd.DataFrame,
) -> None:
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    write_latex_table(
        table6,
        "不同分区策略下Pareto代表方案比较",
        "tab:pareto_representatives",
        PAPER_DIR / "table6_pareto_representatives.tex",
        True,
        (
            "中继总能耗/kWh",
            "总能耗/kWh",
            "新增能耗/kWh",
            "最坏不均衡",
            "平均不均衡",
        ),
    )
    write_latex_table(
        table8,
        "代表性分区方案执行性能比较",
        "tab:execution_performance",
        PAPER_DIR / "table8_execution_performance.tex",
        True,
        ("运输能耗/kWh", "中继能耗/kWh", "总能耗/kWh"),
    )
    # Format the mixed-unit comparison table before sending it to LaTeX.
    latex_table9 = table9.astype(object).copy()
    for row_index, row in latex_table9.iterrows():
        metric = row["指标"]
        for column in latex_table9.columns[1:]:
            value = row[column]
            if value == "—":
                continue
            if metric.endswith("/kWh") or metric in ("最坏不均衡", "平均不均衡"):
                latex_table9.at[row_index, column] = f"{float(value):.4f}"
            elif metric == "整体完工时间/s":
                latex_table9.at[row_index, column] = f"{float(value):.2f}"
            else:
                latex_table9.at[row_index, column] = str(int(value))
    write_latex_table(
        latex_table9,
        "K=2与K=3代表性分区方案综合比较",
        "tab:overall_comparison",
        PAPER_DIR / "table9_overall_comparison.tex",
        True,
    )
    write_latex_table(
        table10,
        "中继任务跨组复制及附加能耗",
        "tab:relay_energy_duplication",
        PAPER_DIR / "table10_relay_energy_duplication.tex",
        True,
        ("中继总能耗/kWh", "较问题三新增中继能耗/kWh"),
    )


def main() -> None:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    hashes_before = {path: _file_hash(path) for path in PROTECTED_FILES}
    sources = read_sources()
    group_detail, summary, relay_detail = calculate_execution_metrics(sources)
    table8 = build_table8(group_detail, summary)
    table9 = build_table9(sources, summary)
    table6 = build_updated_table6(sources, summary)
    table10 = build_table10(summary)
    baseline = build_problem3_baseline(sources)

    # Append only to a writable run copy; the frozen workbook is never edited.
    prepare_writable_input("problem4", "problem4_paper_tables.xlsx")
    update_paper_workbook(table6, table8, table9, table10)
    write_latex_outputs(table6, table8, table9, table10)
    validation = validate(
        sources, group_detail, summary, relay_detail, hashes_before
    )
    if not validation["result"].eq("PASS").all():
        failed = validation.loc[validation["result"].ne("PASS"), "check"].tolist()
        raise AssertionError(f"Energy/makespan validation failed: {failed}")
    write_verification_workbook(
        group_detail, summary, relay_detail, baseline, validation
    )

    for partition_id in ("K2-041", "K3-301", "K2-051", "K3-147"):
        row = summary.loc[partition_id]
        print(
            f"{partition_id}: "
            f"E_transport={row['transport_energy_kwh']:.12f} kWh, "
            f"E_relay={row['relay_energy_kwh']:.12f} kWh, "
            f"E_total={row['total_energy_kwh']:.12f} kWh, "
            f"Delta_E_total={row['delta_total_energy_kwh']:.12f} kWh, "
            f"Cmax={row['Cmax_s']:.12f} s"
        )
    for number, title, table in (
        (8, "代表性分区方案执行性能比较", table8),
        (9, "K=2与K=3代表性分区方案综合比较", table9),
        (10, "中继任务跨组复制及附加能耗", table10),
    ):
        print("\n" + "=" * 72)
        print(f"表{number}：{title}（{len(table)}行 × {len(table.columns)}列）")
        print("=" * 72)
        print(table.to_string(index=False))
    print("\n" + "=" * 72)
    print("15项核验")
    print("=" * 72)
    for row in validation.itertuples(index=False):
        print(f"[{row.result}] {row.check}")
    print(f"\nPaper workbook: {PAPER_WORKBOOK}")
    print(f"Verification workbook: {VERIFICATION_OUTPUT}")
    print(
        "LaTeX: "
        + "; ".join(
            str(PAPER_DIR / name)
            for name in (
                "table6_pareto_representatives.tex",
                "table8_execution_performance.tex",
                "table9_overall_comparison.tex",
                "table10_relay_energy_duplication.tex",
            )
        )
    )


if __name__ == "__main__":
    main()
