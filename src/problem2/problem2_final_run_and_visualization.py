"""问题二正式求解、结果保存与绘图的统一运行入口。

正常使用时只需修改 ``OBJECTIVE_WEIGHTS``，其余正式参数保持固定。
搜索、物理模型、资源调度、目标函数、约束以及 Repair 优化均直接复用
现有实现；本文件只负责参数注入、结果校验、导出和绘图适配。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys

# Keep historical module names while resolving all locations from this repository.
_RELEASE_ROOT = Path(__file__).resolve().parents[2]
if str(_RELEASE_ROOT) not in sys.path:
    sys.path.insert(0, str(_RELEASE_ROOT))
from project_paths import ROOT, bootstrap, raw_file, dem_file, input_file, output_dir, output_file, require_file, assert_writable_output

bootstrap()
from time import perf_counter
from typing import Dict, Tuple
import math

from matplotlib.figure import Figure
import numpy as np
import pandas as pd

import problem2_alns as core
import problem2_alns_improved as imp
import problem2_alns_repair_optimized as optimized


# ---------------------------------------------------------------------------
# 固定正式参数。日常运行只修改 OBJECTIVE_WEIGHTS。
# ---------------------------------------------------------------------------
MAX_ITERATIONS = 1000
REPAIR_TOP_K = 5
T0 = 0.03
ALPHA = 0.98
DESTROY_RATIO_MIN = 0.08
DESTROY_RATIO_MAX = 0.20
RANDOM_SEED = 20260927

OBJECTIVE_WEIGHTS = (0.55, 0.15, 0.15, 0.15)

RESULT_DIR = output_dir("problem2")
TOLERANCE = core.TOLERANCE


def validate_objective_weights(weights: Tuple[float, ...]) -> Tuple[float, ...]:
    """检查四目标权重；不对用户输入做自动归一化或修正。"""
    if len(weights) != 4:
        raise ValueError("OBJECTIVE_WEIGHTS 必须恰好包含4个元素")
    try:
        validated = tuple(float(value) for value in weights)
    except (TypeError, ValueError) as exc:
        raise ValueError("OBJECTIVE_WEIGHTS 的4个元素必须都是数值") from exc
    if any(not math.isfinite(value) or value < 0.0 for value in validated):
        raise ValueError("OBJECTIVE_WEIGHTS 必须均为有限非负数")
    if not math.isclose(sum(validated), 1.0, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError("OBJECTIVE_WEIGHTS 之和必须为1")
    return validated


def identify_scenario(weights: Tuple[float, ...]) -> str:
    """识别五种标准情景，其他合法权重生成稳定的 lambda 名称。"""
    standard = (
        ((0.25, 0.25, 0.25, 0.25), "Balanced"),
        ((0.55, 0.15, 0.15, 0.15), "Z1_priority"),
        ((0.15, 0.55, 0.15, 0.15), "Z2_priority"),
        ((0.15, 0.15, 0.55, 0.15), "Z3_priority"),
        ((0.15, 0.15, 0.15, 0.55), "Z4_priority"),
    )
    for reference, name in standard:
        if all(
            math.isclose(value, target, rel_tol=0.0, abs_tol=1e-9)
            for value, target in zip(weights, reference)
        ):
            return name
    return "lambda_" + "_".join(f"{value:.2f}" for value in weights)


VALIDATED_WEIGHTS = validate_objective_weights(OBJECTIVE_WEIGHTS)
SCENARIO_NAME = identify_scenario(VALIDATED_WEIGHTS)
OUTPUT_FILE = RESULT_DIR / f"problem2_{SCENARIO_NAME}_result.xlsx"


def scenario_figure_label(scenario_name: str) -> str:
    labels = {
        "Balanced": "等权",
        "Z1_priority": "Z1优先",
        "Z2_priority": "Z2优先",
        "Z3_priority": "Z3优先",
        "Z4_priority": "Z4优先",
    }
    return labels.get(scenario_name, scenario_name)


def figure_paths(scenario_name: str) -> Dict[str, Path]:
    prefix = f"问题2_{scenario_figure_label(scenario_name)}"
    return {
        "convergence": RESULT_DIR / f"{prefix}_ALNS收敛曲线.png",
        "routes": RESULT_DIR / f"{prefix}_最终运输路线图.png",
        "uav": RESULT_DIR / f"{prefix}_实体无人机甘特图.png",
        "battery": RESULT_DIR / f"{prefix}_共享电池甘特图.png",
        "delivery": RESULT_DIR / f"{prefix}_送达时间对比图.png",
    }


def console_log(message: str, reset: bool = False) -> None:
    """避免正式入口改写其他实验脚本的既有日志文件。"""
    del reset
    print(message, flush=True)


def verify_final_solution(
    best: core.Evaluation, data: core.ProblemData
) -> pd.DataFrame:
    """在任何最终文件生成前独立执行并强化现有16项硬约束检查。"""
    feasibility = core.check_global_feasibility(best, data)
    assigned = [box_id for sortie in best.solution for box_id in sortie.box_ids]
    expected_count = len(data.boxes)
    pass_count = int((feasibility["结果"] == "PASS").sum())
    checks_ok = (
        len(feasibility) == 16
        and pass_count == 16
        and best.feasible
        and expected_count == 80
        and len(assigned) == 80
        and len(set(assigned)) == 80
        and set(assigned) == set(data.boxes)
        and best.z4_sorties == len(best.solution)
        and best.z4_sorties == len(best.scheduled)
    )
    if not checks_ok:
        raise RuntimeError(
            "最终方案未通过完整校验，已停止且不会生成最终 Excel/PNG："
            f"独立检查={pass_count}/16，货箱={len(assigned)}/{expected_count}，"
            f"Z4={best.z4_sorties}，实际架次={len(best.scheduled)}"
        )
    return feasibility


def build_summary(
    best: core.Evaluation, runtime_s: float, feasibility_label: str
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Scenario": SCENARIO_NAME,
                "Lambda1": VALIDATED_WEIGHTS[0],
                "Lambda2": VALIDATED_WEIGHTS[1],
                "Lambda3": VALIDATED_WEIGHTS[2],
                "Lambda4": VALIDATED_WEIGHTS[3],
                "F": best.objective,
                "Z1": best.z1_weighted_tardiness,
                "Z2": best.z2_makespan_s,
                "Z3": best.z3_energy_kwh,
                "Z4": best.z4_sorties,
                "Number_of_Sorties": len(best.scheduled),
                "Random_Seed": RANDOM_SEED,
                "Max_Iterations": MAX_ITERATIONS,
                "Repair_Top_K": REPAIR_TOP_K,
                "T0": T0,
                "Alpha": ALPHA,
                "Destroy_Ratio_Min": DESTROY_RATIO_MIN,
                "Destroy_Ratio_Max": DESTROY_RATIO_MAX,
                "Runtime_s": runtime_s,
                "Feasibility": feasibility_label,
            }
        ]
    )


def export_results(
    best: core.Evaluation,
    data: core.ProblemData,
    convergence: pd.DataFrame,
    operator_statistics: pd.DataFrame,
    diagnostics: dict,
    repair_stats: pd.DataFrame,
    cache_stats: pd.DataFrame,
    feasibility: pd.DataFrame,
    runtime_s: float,
) -> None:
    """复用既有表生成与格式化逻辑，输出统一命名的最终工作簿。"""
    base_tables = imp.build_result_tables(best, data, runtime_s, diagnostics)
    tables = {
        "Summary": build_summary(best, runtime_s, "16/16 PASS"),
        "Sorties": base_tables["Sorties"],
        "Box_Delivery": base_tables["Boxes"],
        "UAV_Schedule": base_tables["UAV_Usage"],
        "Battery_Schedule": base_tables["Battery_Usage"],
        "Feasibility": feasibility,
        "Repair_Performance": repair_stats,
        "Evaluation_Cache": cache_stats,
        "Operator_Statistics": operator_statistics,
        "ALNS_Log": convergence,
    }
    with pd.ExcelWriter(assert_writable_output(OUTPUT_FILE), engine="openpyxl") as writer:
        for sheet_name, table in tables.items():
            table.to_excel(writer, sheet_name=sheet_name[:31], index=False)
        imp.format_workbook(writer)


def plot_best_convergence(convergence: pd.DataFrame, output_file: Path) -> None:
    """适配 Improved ALNS 日志，仅绘制本次运行的 Best F。"""
    fig, ax = core.plt.subplots(figsize=(9, 5), dpi=180)
    ax.plot(
        convergence["iteration"],
        convergence["best_F"],
        color="#2f78b7",
        linewidth=2.0,
        label="Best F",
    )
    ax.set_xlabel("Iteration")
    ax.set_ylabel("Best F")
    # ax.set_title("问题二 ALNS 收敛曲线")
    ax.grid(alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(assert_writable_output(output_file), bbox_inches="tight")
    core.plt.close(fig)


def plot_uav_gantt_with_cmax(
    evaluation: core.Evaluation, data: core.ProblemData, output_file: Path
) -> None:
    """沿用原甘特图风格，并补充当前方案实际 C_max 标注。"""
    colors = {"A": "#2f78b7", "B": "#ef8a17", "C": "#3a9d5d"}
    uav_ids = [
        uav_id
        for uav_type in core.UAV_TYPES
        for uav_id in data.uav_ids_by_type[uav_type]
    ]
    y_index = {uav_id: index for index, uav_id in enumerate(uav_ids)}
    fig, ax = core.plt.subplots(figsize=(11, 5.5), dpi=180)
    for item in evaluation.scheduled:
        y = y_index[item.uav_id]
        ax.barh(
            y,
            item.completion_s - item.start_s,
            left=item.start_s,
            height=0.55,
            color=colors[item.sortie.uav_type],
            edgecolor="black",
            linewidth=0.4,
        )
        ax.text(
            (item.start_s + item.completion_s) / 2,
            y,
            item.sortie_id,
            ha="center",
            va="center",
            fontsize=7,
            color="white",
        )
    ax.axvline(
        evaluation.z2_makespan_s,
        color="#8c2d2d",
        linestyle="--",
        linewidth=1.3,
        label=f"$C_{{max}}$ = Z2 = {evaluation.z2_makespan_s:.3f} s",
    )
    ax.set_yticks(range(len(uav_ids)), uav_ids)
    ax.set_xlabel("时刻（s）")
    ax.set_ylabel("实体无人机")
    # ax.set_title("实体无人机任务甘特图")
    ax.grid(axis="x", alpha=0.25)
    ax.legend(loc="best")
    fig.tight_layout()
    fig.savefig(assert_writable_output(output_file), bbox_inches="tight")
    core.plt.close(fig)


def plot_delivery_with_deadlines(
    evaluation: core.Evaluation, data: core.ProblemData, output_file: Path
) -> None:
    """沿用原送达图定义，区分期望时刻和两类硬截止时刻。"""
    ordered = sorted(
        data.box_order,
        key=lambda box_id: (
            data.boxes[box_id].expected_time_s,
            data.boxes[box_id].service_zone,
            box_id,
        ),
    )
    actual = [evaluation.delivery_times[box_id] for box_id in ordered]
    expected = [data.boxes[box_id].expected_time_s for box_id in ordered]
    medical_deadlines = [
        min(data.boxes[box_id].hard_deadlines)
        if data.boxes[box_id].material_type == "医疗物资"
        and data.boxes[box_id].hard_deadlines
        else np.nan
        for box_id in ordered
    ]
    first_batch_deadlines = [
        float(data.boxes[box_id].first_deadline_s)
        if data.boxes[box_id].is_first_batch
        else np.nan
        for box_id in ordered
    ]
    x = np.arange(len(ordered))
    fig, ax = core.plt.subplots(figsize=(13, 5.5), dpi=180)
    ax.scatter(x, actual, s=18, label="实际送达时刻", color="#2f78b7")
    ax.plot(x, expected, linewidth=1.2, label="期望送达时刻", color="#d08a35")
    ax.scatter(
        x,
        medical_deadlines,
        s=30,
        marker="D",
        facecolors="none",
        edgecolors="#a33b3b",
        linewidths=0.9,
        label="医疗物资硬时限",
    )
    ax.scatter(
        x,
        first_batch_deadlines,
        s=34,
        marker="^",
        color="#555555",
        label="首批保障货箱截止时刻",
    )
    ax.set_xlabel("按期望时刻排序的货箱序号")
    ax.set_ylabel("时刻（s）")
    # ax.set_title("各货箱实际、期望送达时间与硬截止时间对比")
    ax.grid(alpha=0.2)
    ax.legend()
    fig.tight_layout()
    fig.savefig(assert_writable_output(output_file), bbox_inches="tight")
    core.plt.close(fig)


def plot_results(
    best: core.Evaluation, data: core.ProblemData, convergence: pd.DataFrame
) -> None:
    """复用现有绘图函数，并对列名、输出路径及缺失标注做轻量适配。"""
    paths = figure_paths(SCENARIO_NAME)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    old_paths = (
        core.CONVERGENCE_FIGURE,
        core.ROUTE_FIGURE,
        core.UAV_GANTT_FIGURE,
        core.BATTERY_GANTT_FIGURE,
        core.DELIVERY_FIGURE,
    )
    original_savefig = Figure.savefig

    def savefig_300dpi(figure, *args, **kwargs):
        kwargs.setdefault("dpi", 300)
        return original_savefig(figure, *args, **kwargs)

    core.CONVERGENCE_FIGURE = paths["convergence"]
    core.ROUTE_FIGURE = paths["routes"]
    core.UAV_GANTT_FIGURE = paths["uav"]
    core.BATTERY_GANTT_FIGURE = paths["battery"]
    core.DELIVERY_FIGURE = paths["delivery"]
    Figure.savefig = savefig_300dpi
    try:
        core.setup_chinese_font()
        plot_best_convergence(convergence, paths["convergence"])
        core.plot_routes(best, data)
        plot_uav_gantt_with_cmax(best, data, paths["uav"])
        core.plot_battery_gantt(best, data)
        plot_delivery_with_deadlines(best, data, paths["delivery"])
    finally:
        Figure.savefig = original_savefig
        (
            core.CONVERGENCE_FIGURE,
            core.ROUTE_FIGURE,
            core.UAV_GANTT_FIGURE,
            core.BATTERY_GANTT_FIGURE,
            core.DELIVERY_FIGURE,
        ) = old_paths


def main() -> None:
    """执行正式 Repair-Optimized Improved ALNS 全流程。"""
    imp.CONFIG = replace(
        imp.CONFIG,
        variant="V4",
        random_seed=RANDOM_SEED,
        max_iterations=MAX_ITERATIONS,
        destroy_fraction_min=DESTROY_RATIO_MIN,
        destroy_fraction_max=DESTROY_RATIO_MAX,
        initial_temperature=T0,
        cooling_rate=ALPHA,
        objective_weights=VALIDATED_WEIGHTS,
    )
    started = perf_counter()
    console_log(
        f"Problem 2 final run started: {SCENARIO_NAME}, "
        f"weights={VALIDATED_WEIGHTS}"
    )
    data = core.load_data()
    original_evaluate = imp.evaluate_solution
    original_repairs = dict(imp.REPAIR_OPERATORS)
    original_destroys = dict(imp.DESTROY_OPERATORS)
    original_log = imp.log
    cache = optimized.SharedEvaluationCache(data, original_evaluate)
    engine = optimized.OptimizedRepairEngine(data, cache, REPAIR_TOP_K)
    imp.evaluate_solution = cache.wrapper
    imp.log = console_log
    imp.REPAIR_OPERATORS = {
        "greedy_insertion": engine.greedy,
        "regret2_insertion": engine.regret2,
        "regret3_insertion": engine.regret3,
        "deadline_first_insertion": engine.deadline_first,
    }
    imp.DESTROY_OPERATORS = {
        name: optimized.make_destroy_wrapper(operator, engine)
        for name, operator in original_destroys.items()
    }
    try:
        cache.stage = "initial_construction"
        initial = imp.build_feasible_initial_solution(data)

        cache.stage = "initial_sigma_descent"
        sigma_initial, sigma_initial_diag = imp.sigma_descent(initial, data)

        cache.stage = "alns_outer_local"
        best, convergence, operator_statistics, alns_diag = imp.alns(
            sigma_initial.solution, data
        )

        cache.stage = "final_sigma_descent"
        final_best, sigma_final_diag = imp.sigma_descent(best, data)
        if final_best.objective < best.objective - TOLERANCE:
            best = final_best

        runtime_s = perf_counter() - started
        feasibility = verify_final_solution(best, data)

        repair_stats = engine.table()
        cache_stats = optimized.cache_table(cache)
        diagnostics = dict(alns_diag)
        diagnostics.update(
            {
                "repair_top_k": REPAIR_TOP_K,
                "shared_cache_requests": cache.requests,
                "shared_cache_hits": cache.hits,
                "shared_cache_misses_full_f": cache.misses,
                "initial_sigma_evaluations": sigma_initial_diag[
                    "sigma_descent_evaluations"
                ],
                "final_sigma_evaluations": sigma_final_diag[
                    "sigma_descent_evaluations"
                ],
            }
        )
        RESULT_DIR.mkdir(parents=True, exist_ok=True)
        export_results(
            best,
            data,
            convergence,
            operator_statistics,
            diagnostics,
            repair_stats,
            cache_stats,
            feasibility,
            runtime_s,
        )
        plot_results(best, data, convergence)
        console_log(
            f"Completed: F={best.objective:.12f}, feasibility=16/16 PASS, "
            f"result={OUTPUT_FILE}"
        )
    finally:
        imp.evaluate_solution = original_evaluate
        imp.REPAIR_OPERATORS = original_repairs
        imp.DESTROY_OPERATORS = original_destroys
        imp.log = original_log


if __name__ == "__main__":
    main()
