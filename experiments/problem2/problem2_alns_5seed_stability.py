"""Repair-Optimized Improved ALNS：1000 iterations × 5 seeds 稳定性实验。

不修改任何搜索或物理模型参数。seed=20260924 直接复用既有收敛实验，
只顺序运行 20260925--20260928；每完成一个 seed 立即保存独立结果。
"""

from __future__ import annotations

from pathlib import Path
import sys

# Keep historical module names while resolving all locations from this repository.
_RELEASE_ROOT = Path(__file__).resolve().parents[2]
if str(_RELEASE_ROOT) not in sys.path:
    sys.path.insert(0, str(_RELEASE_ROOT))
from project_paths import ROOT, bootstrap, raw_file, dem_file, input_file, output_dir, output_file, require_file, assert_writable_output

bootstrap()
from typing import Dict, List
import gc
import math

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import problem2_alns_improved as imp
import problem2_alns_iteration_convergence as conv


BASE_DIR = ROOT
RESULT_DIR = output_dir("problem2")
RUN_DIR = RESULT_DIR / "alns_5seed_runs"
EXISTING_FILE = input_file("problem2", 'problem2_alns_iteration_convergence.xlsx')
EXISTING_TRACE = input_file("problem2", 'problem2_alns_iteration_trace.csv')

OUTPUT_FILE = RESULT_DIR / "problem2_alns_5seed_stability.xlsx"
SUMMARY_CSV = RESULT_DIR / "problem2_alns_5seed_summary.csv"
TRACE_CSV = RESULT_DIR / "problem2_alns_5seed_trace.csv"
CONVERGENCE_PNG = RESULT_DIR / "problem2_alns_5seeds_convergence.png"
FINAL_F_PNG = RESULT_DIR / "problem2_alns_5seeds_finalF.png"
LOG_FILE = RESULT_DIR / "problem2_alns_5seed_stability.log"

SEEDS = (20260924, 20260925, 20260926, 20260927, 20260928)
NEW_SEEDS = SEEDS[1:]
MAX_ITERATIONS = 1000


def log(message: str, reset: bool = False) -> None:
    print(message, flush=True)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("w" if reset else "a", encoding="utf-8") as stream:
        stream.write(message + "\n")
        stream.flush()


def sa_table(trace: pd.DataFrame, seed: int) -> pd.DataFrame:
    rows = []
    for start, end in ((1, 120), (121, 500), (501, 1000)):
        part = trace[(trace.iteration >= start) & (trace.iteration <= end)]
        candidates = int(part.candidate_present.sum())
        worse = int(part.candidate_worse.sum())
        rows.append(
            {
                "Seed": seed,
                "Interval": f"{start}-{end}",
                "Candidate_Count": candidates,
                "Acceptance_Rate": (
                    float(part.accepted.sum()) / candidates if candidates else 0.0
                ),
                "Worse_Acceptance_Rate": (
                    float(part.accepted_worse.sum()) / worse if worse else 0.0
                ),
                "Improving_Candidate_Rate": (
                    float(part.is_improvement.sum()) / candidates
                    if candidates
                    else 0.0
                ),
                "Global_Best_Updates": int(part.new_global_best.sum()),
                "Temperature_Start": float(part.temperature.iloc[0]),
                "Temperature_End": float(part.temperature.iloc[-1]),
            }
        )
    return pd.DataFrame(rows)


def cache_numbers(cache_table: pd.DataFrame) -> dict:
    total = cache_table.loc[cache_table.Stage == "TOTAL"].iloc[0]
    lookup = cache_table.set_index("Stage")

    def misses(stage: str) -> int:
        return (
            int(lookup.loc[stage, "Full_F_Evaluations"])
            if stage in lookup.index
            else 0
        )

    repair_full = int(
        cache_table.loc[
            cache_table.Stage.str.startswith("repair:"), "Full_F_Evaluations"
        ].sum()
    )
    return {
        "Initial_Full_F": misses("initial_construction"),
        "Initial_Sigma_Full_F": misses("initial_sigma_descent"),
        "Repair_Full_F": repair_full,
        "Local_Search_Full_F": misses("local_search"),
        "Final_Sigma_Full_F": misses("final_sigma_descent"),
        "Total_Full_F": int(total["Full_F_Evaluations"]),
        "Cache_Hits": int(total["Cache_Hits"]),
    }


def standardized_summary(
    seed: int,
    trace: pd.DataFrame,
    final_row: pd.Series,
    best_updates: pd.DataFrame,
    cache_stats: pd.DataFrame,
    feasibility: pd.DataFrame,
    runtime_s: float,
    solution_saved: bool,
) -> pd.DataFrame:
    loop = trace.iloc[-1]
    final_f = float(final_row["new_best_F"])
    loop_f = float(loop.best_F)
    alns_updates = int(trace.new_global_best.sum())
    alns_update_rows = best_updates[
        (best_updates.iteration > 0)
        & (best_updates.source != "final_sigma_descent")
    ]
    last_iteration = (
        int(alns_update_rows.iteration.max()) if len(alns_update_rows) else 0
    )
    checkpoint = {
        value: float(trace.loc[trace.iteration == value, "best_F"].iloc[0])
        for value in (120, 500, 1000)
    }
    cache_values = cache_numbers(cache_stats)
    pass_count = int((feasibility["结果"] == "PASS").sum())
    sigma_abs = loop_f - final_f
    return pd.DataFrame(
        [
            {
                "Seed": seed,
                "Loop_Best_F": loop_f,
                "Final_F": final_f,
                "Loop_Z1": float(loop.Z1_best),
                "Loop_Z2": float(loop.Z2_best),
                "Loop_Z3": float(loop.Z3_best),
                "Loop_Z4": int(loop.Z4_best),
                "Z1": float(final_row.Z1),
                "Z2": float(final_row.Z2),
                "Z3": float(final_row.Z3),
                "Z4": int(final_row.Z4),
                "Sigma_Improvement": sigma_abs,
                "Sigma_Improvement_Pct": (
                    sigma_abs / loop_f * 100.0 if loop_f else 0.0
                ),
                "Final_Sigma_Improved": sigma_abs > 1e-12,
                "Last_ALNS_Best_Iteration": last_iteration,
                "ALNS_Global_Best_Updates": alns_updates,
                "Best_F_120": checkpoint[120],
                "Best_F_500": checkpoint[500],
                "Best_F_1000_Loop": checkpoint[1000],
                "Runtime_s": runtime_s,
                "Runtime_per_Iteration_s": runtime_s / MAX_ITERATIONS,
                **cache_values,
                "Repair_Full_F_Pct": (
                    cache_values["Repair_Full_F"]
                    / cache_values["Total_Full_F"]
                    * 100.0
                    if cache_values["Total_Full_F"]
                    else 0.0
                ),
                "Feasibility_PASS_Count": pass_count,
                "Feasibility": "16/16 PASS" if pass_count == 16 else "INFEASIBLE",
                "Final_Solution_Detail_Saved": solution_saved,
            }
        ]
    )


def add_seed(table: pd.DataFrame, seed: int) -> pd.DataFrame:
    result = table.copy()
    result.insert(0, "Seed", seed)
    return result


def repair_with_new_best(
    repair: pd.DataFrame, operators: pd.DataFrame, seed: int
) -> pd.DataFrame:
    result = add_seed(repair, seed)
    repair_new_best = operators[operators.category == "repair"].set_index("operator")
    result["New_Best"] = result.Repair.map(repair_new_best.new_best).fillna(0).astype(int)
    return result


def save_seed_package(package: Dict[str, pd.DataFrame], seed: int) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    xlsx = RUN_DIR / f"seed_{seed}.xlsx"
    trace_csv = RUN_DIR / f"seed_{seed}_trace.csv"
    package["Trace"].to_csv(trace_csv, index=False, encoding="utf-8-sig")
    with pd.ExcelWriter(assert_writable_output(xlsx), engine="openpyxl") as writer:
        for name in (
            "Summary",
            "Best_Updates",
            "Operator_Statistics",
            "Repair_Statistics",
            "SA_Statistics",
            "Cache_Statistics",
            "Feasibility",
            "Final_Sorties",
            "Final_Boxes",
            "UAV_Usage",
            "Battery_Usage",
        ):
            package[name].to_excel(writer, sheet_name=name[:31], index=False)
        imp.format_workbook(writer)
    log(f"seed={seed} 已保存: {xlsx.name}, {trace_csv.name}")


def load_existing_seed() -> Dict[str, pd.DataFrame]:
    if not EXISTING_FILE.exists() or not EXISTING_TRACE.exists():
        raise FileNotFoundError("缺少 seed=20260924 的既有1000轮实验结果")
    trace = pd.read_csv(require_file(EXISTING_TRACE))
    best_updates = pd.read_excel(require_file(EXISTING_FILE), sheet_name="Best_Updates")
    operators = pd.read_excel(require_file(EXISTING_FILE), sheet_name="Operator_Stats")
    repair = pd.read_excel(require_file(EXISTING_FILE), sheet_name="Repair_Performance")
    cache = pd.read_excel(require_file(EXISTING_FILE), sheet_name="Evaluation_Cache")
    feasibility = pd.read_excel(require_file(EXISTING_FILE), sheet_name="Feasibility")
    checkpoints = pd.read_excel(require_file(EXISTING_FILE), sheet_name="Checkpoints")
    final_row = best_updates.iloc[-1]
    runtime = float(checkpoints.loc[checkpoints.Iterations == 1000, "Runtime_s"].iloc[0])
    summary = standardized_summary(
        SEEDS[0], trace, final_row, best_updates, cache, feasibility, runtime, False
    )
    package = {
        "Summary": summary,
        "Trace": add_seed(trace, SEEDS[0]),
        "Best_Updates": add_seed(best_updates, SEEDS[0]),
        "Operator_Statistics": add_seed(operators, SEEDS[0]),
        "Repair_Statistics": repair_with_new_best(repair, operators, SEEDS[0]),
        "SA_Statistics": sa_table(trace, SEEDS[0]),
        "Cache_Statistics": add_seed(cache, SEEDS[0]),
        "Feasibility": add_seed(feasibility, SEEDS[0]),
        "Final_Sorties": pd.DataFrame(
            [{"Seed": SEEDS[0], "Note": "既有收敛实验未序列化最终方案明细；未违规重跑"}]
        ),
        "Final_Boxes": pd.DataFrame(),
        "UAV_Usage": pd.DataFrame(),
        "Battery_Usage": pd.DataFrame(),
    }
    save_seed_package(package, SEEDS[0])
    return package


def run_new_seed(seed: int) -> Dict[str, pd.DataFrame]:
    log(f"\n===== seed={seed} 开始 =====")
    conv.RANDOM_SEED = seed
    conv.LOG_FILE = LOG_FILE
    result = conv.run_single_trajectory()
    trace = result["trace"]
    best_updates = result["best_updates"]
    operators = result["operator_stats"]
    repair = result["repair_stats"]
    cache = result["cache_stats"]
    feasibility = result["feasibility"]
    final_row = best_updates.iloc[-1]
    runtime = float(result["checkpoint_records"][1000]["elapsed_time"])
    result_tables = imp.build_result_tables(
        result["best"],
        result["data"],
        runtime,
        {"Seed": seed, "Experiment": "1000_iterations_5seed_stability"},
    )
    summary = standardized_summary(
        seed, trace, final_row, best_updates, cache, feasibility, runtime, True
    )
    package = {
        "Summary": summary,
        "Trace": add_seed(trace, seed),
        "Best_Updates": add_seed(best_updates, seed),
        "Operator_Statistics": add_seed(operators, seed),
        "Repair_Statistics": repair_with_new_best(repair, operators, seed),
        "SA_Statistics": sa_table(trace, seed),
        "Cache_Statistics": add_seed(cache, seed),
        "Feasibility": add_seed(feasibility, seed),
        "Final_Sorties": add_seed(result_tables["Sorties"], seed),
        "Final_Boxes": add_seed(result_tables["Boxes"], seed),
        "UAV_Usage": add_seed(result_tables["UAV_Usage"], seed),
        "Battery_Usage": add_seed(result_tables["Battery_Usage"], seed),
    }
    save_seed_package(package, seed)
    row = summary.iloc[0]
    log(
        f"seed={seed} 完成: loop={row.Loop_Best_F:.12f}, "
        f"final={row.Final_F:.12f}, last_best={row.Last_ALNS_Best_Iteration}, "
        f"runtime={row.Runtime_s:.3f}s, {row.Feasibility}"
    )
    del result
    gc.collect()
    return package


def concat(packages: List[Dict[str, pd.DataFrame]], key: str) -> pd.DataFrame:
    tables = [package[key] for package in packages if not package[key].empty]
    return pd.concat(tables, ignore_index=True) if tables else pd.DataFrame()


def descriptive_statistics(summary: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for column in ("Final_F", "Z1", "Z2", "Z3", "Z4"):
        values = summary[column].astype(float)
        mean = float(values.mean())
        std = float(values.std(ddof=1))
        rows.append(
            {
                "Metric": column,
                "Min": float(values.min()),
                "Mean": mean,
                "Median": float(values.median()),
                "Sample_Std": std,
                "Max": float(values.max()),
                "CV_Pct": std / mean * 100.0 if mean else math.nan,
            }
        )
    last = summary.Last_ALNS_Best_Iteration.astype(float)
    rows.append(
        {
            "Metric": "Last_ALNS_Best_Iteration",
            "Min": float(last.min()),
            "Mean": float(last.mean()),
            "Median": float(last.median()),
            "Sample_Std": float(last.std(ddof=1)),
            "Max": float(last.max()),
            "CV_Pct": float(last.std(ddof=1) / last.mean() * 100.0),
        }
    )
    sigma = summary.Sigma_Improvement_Pct.astype(float)
    rows.append(
        {
            "Metric": "Sigma_Improvement_Pct",
            "Min": float(sigma.min()),
            "Mean": float(sigma.mean()),
            "Median": float(sigma.median()),
            "Sample_Std": float(sigma.std(ddof=1)),
            "Max": float(sigma.max()),
            "CV_Pct": (
                float(sigma.std(ddof=1) / sigma.mean() * 100.0)
                if sigma.mean()
                else math.nan
            ),
        }
    )
    return pd.DataFrame(rows)


def aggregate_operators(table: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (category, operator), group in table.groupby(["category", "operator"]):
        rows.append(
            {
                "Category": category,
                "Operator": operator,
                "Calls": int(group.calls.sum()),
                "Feasible": int(group.feasible.sum()),
                "Accepted": int(group.accepted.sum()),
                "Improved": int(group.improved.sum()),
                "New_Best": int(group.new_best.sum()),
                "Total_Improvement": float(group.total_improvement.sum()),
                "Seeds_with_New_Best": int((group.new_best > 0).sum()),
                "Mean_Final_Weight": float(group.final_weight.mean()),
            }
        )
    return pd.DataFrame(rows)


def aggregate_repairs(table: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for repair, group in table.groupby("Repair"):
        old_estimate = int(group.Old_Full_F_Estimate.sum())
        saved = int(group.Full_F_Saved.sum())
        rows.append(
            {
                "Repair": repair,
                "Calls": int(group.Calls.sum()),
                "Generated": int(group.Generated.sum()),
                "Screened_Out": int(group.Screened_Out.sum()),
                "Full_F_Requests": int(group.Full_F_Requests.sum()),
                "Cache_Hits": int(group.Cache_Hits.sum()),
                "Actual_Full_F": int(group.Cache_Misses_Full_F.sum()),
                "Old_Full_F_Estimate": old_estimate,
                "Full_F_Saved": saved,
                "Saving_Rate": saved / old_estimate if old_estimate else 0.0,
                "Mean_Seed_Saving_Rate": float(group.Saving_Rate.mean()),
                "Runtime_s": float(group.Runtime_s.sum()),
                "Mean_Runtime_s": float(group.Runtime_s.mean()),
                "New_Best": int(group.New_Best.sum()),
                "Seeds_with_New_Best": int((group.New_Best > 0).sum()),
            }
        )
    return pd.DataFrame(rows)


def make_plots(trace: pd.DataFrame, summary: pd.DataFrame) -> None:
    plt.figure(figsize=(10.5, 6))
    colors = plt.cm.tab10(np.linspace(0, 1, len(SEEDS)))
    for color, seed in zip(colors, SEEDS):
        part = trace[trace.Seed == seed]
        plt.plot(part.iteration, part.best_F, linewidth=1.35, color=color, label=str(seed))
        final_f = float(summary.loc[summary.Seed == seed, "Final_F"].iloc[0])
        plt.scatter([1000], [final_f], color=color, marker="D", s=28, zorder=4)
    plt.xlabel("Iteration")
    plt.ylabel("Best F")
    plt.title("Improved ALNS: 5-Seed Global-Best Convergence")
    plt.legend(title="Seed", ncol=2)
    plt.grid(alpha=0.25)
    plt.tight_layout()
    plt.savefig(assert_writable_output(CONVERGENCE_PNG), dpi=220)
    plt.close()

    ordered = summary.sort_values("Seed")
    mean_f = float(ordered.Final_F.mean())
    plt.figure(figsize=(9, 5.5))
    plt.bar(ordered.Seed.astype(str), ordered.Final_F, color="#4c78a8")
    plt.axhline(mean_f, color="#c62828", linestyle="--", linewidth=1.5, label=f"Mean={mean_f:.6f}")
    plt.xlabel("Random Seed")
    plt.ylabel("Final F")
    plt.title("Improved ALNS Final F Across 5 Seeds")
    plt.legend()
    plt.grid(axis="y", alpha=0.25)
    plt.tight_layout()
    plt.savefig(assert_writable_output(FINAL_F_PNG), dpi=220)
    plt.close()


def export_aggregate(packages: List[Dict[str, pd.DataFrame]]) -> None:
    summary = concat(packages, "Summary").sort_values("Seed")
    trace = concat(packages, "Trace").sort_values(["Seed", "iteration"])
    updates = concat(packages, "Best_Updates").sort_values(["Seed", "iteration"])
    operators = concat(packages, "Operator_Statistics")
    repairs = concat(packages, "Repair_Statistics")
    sa = concat(packages, "SA_Statistics").sort_values(["Seed", "Interval"])
    feasibility = concat(packages, "Feasibility").sort_values(["Seed", "检查序号"])
    final_sorties = concat(packages, "Final_Sorties")
    final_boxes = concat(packages, "Final_Boxes")
    uav_usage = concat(packages, "UAV_Usage")
    battery_usage = concat(packages, "Battery_Usage")
    stats = descriptive_statistics(summary)
    operator_aggregate = aggregate_operators(operators)
    repair_aggregate = aggregate_repairs(repairs)

    summary.to_csv(SUMMARY_CSV, index=False, encoding="utf-8-sig")
    trace.to_csv(TRACE_CSV, index=False, encoding="utf-8-sig")
    make_plots(trace, summary)
    with pd.ExcelWriter(assert_writable_output(OUTPUT_FILE), engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        summary.to_excel(writer, sheet_name="Seed_Details", index=False)
        stats.to_excel(writer, sheet_name="Objective_Statistics", index=False)
        updates.to_excel(writer, sheet_name="Best_Updates", index=False)
        operator_aggregate.to_excel(writer, sheet_name="Operator_Statistics", index=False)
        operators.to_excel(writer, sheet_name="Operator_By_Seed", index=False)
        repair_aggregate.to_excel(writer, sheet_name="Repair_Statistics", index=False)
        repairs.to_excel(writer, sheet_name="Repair_By_Seed", index=False)
        sa.to_excel(writer, sheet_name="SA_Statistics", index=False)
        feasibility.to_excel(writer, sheet_name="Feasibility", index=False)
        final_sorties.to_excel(writer, sheet_name="Final_Solutions", index=False)
        final_boxes.to_excel(writer, sheet_name="Final_Boxes", index=False)
        uav_usage.to_excel(writer, sheet_name="UAV_Usage", index=False)
        battery_usage.to_excel(writer, sheet_name="Battery_Usage", index=False)
        imp.format_workbook(writer)
    log("\n===== 五个 seed 汇总 =====")
    log(summary.to_string(index=False))
    log("\nObjective statistics:\n" + stats.to_string(index=False))
    log("\nOperator aggregate:\n" + operator_aggregate.to_string(index=False))
    log("\nRepair aggregate:\n" + repair_aggregate.to_string(index=False))
    log(f"aggregate={OUTPUT_FILE}")


def main() -> None:
    log(
        "5-seed稳定性实验开始：复用20260924，只顺序运行20260925--20260928",
        reset=True,
    )
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    packages: List[Dict[str, pd.DataFrame]] = [load_existing_seed()]
    for seed in NEW_SEEDS:
        packages.append(run_new_seed(seed))
    export_aggregate(packages)
    log("实验结束：未运行其他seed、迭代数或参数组合")


if __name__ == "__main__":
    main()
