"""绘制G01直接通信覆盖区域与DEM地形分布图。

通信状态逐点调用 problem3_communication.evaluate_ug_link，完整保留
WGS84三维距离、FSPL、DEM三维视线遮挡、附加损耗和双向链路预算口径。
图中AGL仅表示测试点相对当地DEM地面的假定飞行高度，不代表实际任务轨迹。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import List, Sequence, Tuple
import math
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap
from matplotlib import font_manager
from matplotlib.lines import Line2D
import numpy as np


# Preserve historical module names used by trusted communication caches.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
import project_paths as paths
paths.bootstrap()
MODULE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = paths.ROOT

import problem3_communication as communication  # noqa: E402
import problem3_core as core  # noqa: E402


# 30 m DEM每隔25个像元取一个测试点，水平采样间距约750 m。
# 采用单进程，避免与正在运行的正式ALNS争抢CPU资源。
COMM_MAP_STRIDE = 25
TEST_AGL_HEIGHTS_M: Tuple[float, ...] = (50.0, 100.0, 200.0, 300.0)
VALIDATION_POINT_COUNT = 12
OUTPUT_PNG = paths.output_file("problem3", "problem3_g01_communication_region.png")
OUTPUT_PDF = paths.output_file("problem3", "problem3_g01_communication_region.pdf")


@dataclass(frozen=True)
class CoverageResult:
    agl_m: float
    coverage: np.ndarray
    sampled_points: int
    connected_points: int
    disconnected_points: int
    runtime_s: float


def setup_chinese_font() -> None:
    """优先使用Windows常见中文字体，避免论文图中文乱码。"""
    candidates = (
        "Microsoft YaHei",
        "SimHei",
        "SimSun",
        "Noto Sans CJK SC",
    )
    installed = {font.name for font in font_manager.fontManager.ttflist}
    for name in candidates:
        if name in installed:
            plt.rcParams["font.sans-serif"] = [name]
            break
    else:
        raise RuntimeError("未找到可用中文字体，停止输出以避免中文乱码")
    plt.rcParams["axes.unicode_minus"] = False


def sampled_grid(
    dem: communication.DemData,
    stride: int = COMM_MAP_STRIDE,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """返回采样行列、像元中心经纬度和对应DEM地面高程。"""
    if stride < 1:
        raise ValueError("COMM_MAP_STRIDE必须为正整数")
    height, width = dem.elevation_m.shape
    rows = np.arange(stride // 2, height, stride, dtype=np.int32)
    cols = np.arange(stride // 2, width, stride, dtype=np.int32)
    col_grid, row_grid = np.meshgrid(cols, rows)
    longitude, latitude = dem.transform * (
        col_grid.astype(float) + 0.5,
        row_grid.astype(float) + 0.5,
    )
    ground = dem.elevation_m[row_grid, col_grid]
    return rows, cols, np.asarray(longitude), np.asarray(latitude), ground


def calculate_coverage(
    agl_m: float,
    longitude: np.ndarray,
    latitude: np.ndarray,
    ground: np.ndarray,
    gateway: communication.GatewayEndpoint,
    parameters: communication.CommunicationParameters,
    dem: communication.DemData,
) -> CoverageResult:
    """调用正式UG接口计算一个AGL高度下的规则网格覆盖矩阵。"""
    started = perf_counter()
    coverage = np.full(ground.shape, np.nan, dtype=np.float64)
    valid_indexes = np.argwhere(np.isfinite(ground))
    connected = 0
    for row_index, col_index in valid_indexes:
        point = communication.Point3D(
            longitude_deg=float(longitude[row_index, col_index]),
            latitude_deg=float(latitude[row_index, col_index]),
            altitude_m=float(ground[row_index, col_index] + agl_m),
        )
        available = communication.evaluate_ug_link(
            point, gateway.point, parameters, dem
        ).available
        coverage[row_index, col_index] = float(available)
        connected += int(available)
    sampled = int(len(valid_indexes))
    return CoverageResult(
        agl_m=agl_m,
        coverage=coverage,
        sampled_points=sampled,
        connected_points=connected,
        disconnected_points=sampled - connected,
        runtime_s=perf_counter() - started,
    )


def validate_against_standard_function(
    results: Sequence[CoverageResult],
    longitude: np.ndarray,
    latitude: np.ndarray,
    ground: np.ndarray,
    gateway: communication.GatewayEndpoint,
    parameters: communication.CommunicationParameters,
    dem: communication.DemData,
) -> None:
    """固定抽取至少10个地图网格状态，重新调用正式UG函数逐点复核。"""
    valid_flat = np.flatnonzero(np.isfinite(ground.ravel()))
    if len(valid_flat) < VALIDATION_POINT_COUNT:
        raise RuntimeError("有效DEM采样点不足，无法进行数值复核")
    selected = valid_flat[
        np.linspace(
            0, len(valid_flat) - 1, VALIDATION_POINT_COUNT, dtype=np.int64
        )
    ]
    checked = 0
    for validation_index, flat_index in enumerate(selected):
        result = results[validation_index % len(results)]
        row_index, col_index = np.unravel_index(flat_index, ground.shape)
        point = communication.Point3D(
            longitude_deg=float(longitude[row_index, col_index]),
            latitude_deg=float(latitude[row_index, col_index]),
            altitude_m=float(
                ground[row_index, col_index] + result.agl_m
            ),
        )
        standard = communication.evaluate_ug_link(
            point, gateway.point, parameters, dem
        ).available
        mapped = bool(result.coverage[row_index, col_index])
        if mapped != standard:
            raise RuntimeError(
                "通信覆盖矩阵与正式UG函数状态不一致: "
                f"AGL={result.agl_m:.0f}m, row={row_index}, "
                f"col={col_index}, map={mapped}, standard={standard}"
            )
        checked += 1
    print(
        f"Numerical validation: {checked}/{checked} sampled states agree "
        "with problem3_communication.evaluate_ug_link.",
        flush=True,
    )


def plot_coverage_map(
    dem: communication.DemData,
    longitude: np.ndarray,
    latitude: np.ndarray,
    results: Sequence[CoverageResult],
    gateway: communication.GatewayEndpoint,
    nodes: dict[str, core.Node3D],
) -> None:
    """绘制统一地形色标的2×2论文图。"""
    valid_elevation = dem.elevation_m[np.isfinite(dem.elevation_m)]
    elevation_min = float(np.nanmin(valid_elevation))
    elevation_max = float(np.nanmax(valid_elevation))
    extent = (
        float(dem.bounds.left),
        float(dem.bounds.right),
        float(dem.bounds.bottom),
        float(dem.bounds.top),
    )
    terrain_cmap = plt.get_cmap("terrain")
    # 洋红覆盖层与terrain色带中的蓝/绿/黄均有较强对比。
    coverage_color = "#D81B60"
    coverage_cmap = ListedColormap([coverage_color])
    figure, axes = plt.subplots(
        2, 2, figsize=(12.2, 9.6), sharex=True, sharey=True,
        constrained_layout=True,
    )
    panel_labels = ("(a)", "(b)", "(c)", "(d)")
    terrain_image = None
    service_nodes = [
        node for node_id, node in sorted(nodes.items()) if node_id != "O01"
    ]
    service_lon = [node.longitude_deg for node in service_nodes]
    service_lat = [node.latitude_deg for node in service_nodes]

    for axis, label, result in zip(axes.ravel(), panel_labels, results):
        terrain_image = axis.imshow(
            dem.elevation_m,
            extent=extent,
            origin="upper",
            cmap=terrain_cmap,
            vmin=elevation_min,
            vmax=elevation_max,
            interpolation="bilinear",
            rasterized=True,
            zorder=1,
        )
        available = np.ma.masked_where(
            ~np.isfinite(result.coverage) | (result.coverage < 0.5),
            np.ones_like(result.coverage),
        )
        axis.pcolormesh(
            longitude,
            latitude,
            available,
            shading="nearest",
            cmap=coverage_cmap,
            vmin=0.0,
            vmax=1.0,
            alpha=0.52,
            rasterized=True,
            zorder=2,
        )
        axis.contour(
            longitude,
            latitude,
            np.ma.masked_invalid(result.coverage),
            levels=[0.5],
            colors=["#8E0152"],
            linewidths=0.55,
            zorder=3,
        )
        axis.scatter(
            service_lon,
            service_lat,
            s=18,
            marker="o",
            facecolor="white",
            edgecolor="black",
            linewidth=0.65,
            zorder=4,
        )
        for node in service_nodes:
            axis.annotate(
                node.node_id,
                (node.longitude_deg, node.latitude_deg),
                xytext=(3, 3),
                textcoords="offset points",
                fontsize=5.8,
                color="black",
                zorder=5,
            )
        depot = nodes["O01"]
        axis.scatter(
            depot.longitude_deg,
            depot.latitude_deg,
            s=46,
            marker="s",
            facecolor="none",
            edgecolor="black",
            linewidth=1.2,
            zorder=6,
        )
        axis.scatter(
            gateway.point.longitude_deg,
            gateway.point.latitude_deg,
            s=105,
            marker="*",
            facecolor="#D62728",
            edgecolor="white",
            linewidth=0.8,
            zorder=7,
        )
        axis.annotate(
            "G01",
            (gateway.point.longitude_deg, gateway.point.latitude_deg),
            xytext=(6, 7),
            textcoords="offset points",
            fontsize=8,
            weight="bold",
            color="#9E1B1B",
            zorder=8,
        )
        axis.annotate(
            "O01",
            (depot.longitude_deg, depot.latitude_deg),
            xytext=(-22, -12),
            textcoords="offset points",
            fontsize=7,
            color="black",
            zorder=8,
        )
        axis.set_title(
            f"{label} 无人机离地高度：{result.agl_m:.0f} m",
            fontsize=11,
        )
        axis.set_xlim(extent[0], extent[1])
        axis.set_ylim(extent[2], extent[3])
        axis.grid(color="white", linewidth=0.35, alpha=0.35)
        axis.tick_params(axis="both", labelsize=8)
        axis.set_aspect("equal", adjustable="box")

    for axis in axes[:, 0]:
        axis.set_ylabel("纬度（°N）", fontsize=10)
    for axis in axes[-1, :]:
        axis.set_xlabel("经度（°E）", fontsize=10)
    # figure.suptitle("地形影响下 G01 直接通信覆盖区域",fontsize=14,weight="semibold",)
    if terrain_image is None:
        raise AssertionError("DEM底图未生成")
    colorbar = figure.colorbar(
        terrain_image,
        ax=axes.ravel().tolist(),
        orientation="vertical",
        shrink=0.86,
        pad=0.02,
    )
    colorbar.set_label("地面高程 / m", fontsize=10)
    colorbar.ax.tick_params(labelsize=8)
    legend_handles = [
        Line2D(
            [0], [0], marker="*", color="none", markerfacecolor="#D62728",
            markeredgecolor="white", markersize=12, label="G01 固定网关",
        ),
        Line2D(
            [0], [0], marker="s", color="none", markerfacecolor="none",
            markeredgecolor="black", markersize=7, label="O01 调度中心",
        ),
        Line2D(
            [0], [0], marker="o", color="none", markerfacecolor="white",
            markeredgecolor="black", markersize=6, label="服务区",
        ),
        Line2D(
            [0], [0], marker="s", color="none", markerfacecolor=coverage_color,
            markeredgecolor="none", alpha=0.55, markersize=9,
            label="G01 直接通信可达",
        ),
    ]
    figure.legend(
        handles=legend_handles,
        loc="lower center",
        bbox_to_anchor=(0.5, -0.055),
        ncol=4,
        frameon=True,
        fontsize=9,
    )
    paths.ensure_output_path(OUTPUT_PNG)
    OUTPUT_PNG.parent.mkdir(parents=True, exist_ok=True)
    paths.ensure_output_path(OUTPUT_PNG)
    paths.ensure_output_path(OUTPUT_PDF)
    figure.savefig(OUTPUT_PNG, dpi=400, bbox_inches="tight")
    figure.savefig(OUTPUT_PDF, bbox_inches="tight")
    plt.close(figure)


def main() -> None:
    total_started = perf_counter()
    setup_chinese_font()
    parameters = communication.load_communication_parameters()
    gateway = communication.load_g01_endpoint(parameters)
    dem = communication.load_dem()
    nodes = core.load_nodes()
    thresholds = communication.standard_link_thresholds_db(parameters)
    if not math.isclose(gateway.ground_elevation_m, 127.7, abs_tol=1e-9):
        raise RuntimeError("G01 Excel地面海拔不是127.7 m")
    if not math.isclose(gateway.antenna_height_agl_m, 20.0, abs_tol=1e-9):
        raise RuntimeError("G01天线离地高度不是20 m")
    if not math.isclose(gateway.point.altitude_m, 147.7, abs_tol=1e-9):
        raise RuntimeError("G01天线绝对海拔不是147.7 m")
    if not math.isclose(thresholds["UG"], 122.0, abs_tol=1e-9):
        raise RuntimeError("正式UG双向最大允许总传播损耗不是122 dB")

    rows, cols, longitude, latitude, ground = sampled_grid(dem)
    print(
        f"COMM_MAP_STRIDE={COMM_MAP_STRIDE}; DEM={dem.elevation_m.shape}; "
        f"sample_grid={len(rows)}x{len(cols)}; "
        f"G01_ground={gateway.ground_elevation_m:.1f}m; "
        f"G01_antenna_altitude={gateway.point.altitude_m:.1f}m; "
        f"Lmax_UG={thresholds['UG']:.0f}dB",
        flush=True,
    )
    results: List[CoverageResult] = []
    for agl_m in TEST_AGL_HEIGHTS_M:
        result = calculate_coverage(
            agl_m,
            longitude,
            latitude,
            ground,
            gateway,
            parameters,
            dem,
        )
        results.append(result)
        percentage = (
            100.0 * result.connected_points / result.sampled_points
            if result.sampled_points else 0.0
        )
        print(
            f"AGL={agl_m:.0f}m: sampled={result.sampled_points}, "
            f"connected={result.connected_points}, "
            f"disconnected={result.disconnected_points}, "
            f"connected_percentage={percentage:.6f}%, "
            f"runtime={result.runtime_s:.3f}s",
            flush=True,
        )

    validate_against_standard_function(
        results,
        longitude,
        latitude,
        ground,
        gateway,
        parameters,
        dem,
    )
    plot_coverage_map(dem, longitude, latitude, results, gateway, nodes)
    print(f"PNG={OUTPUT_PNG}", flush=True)
    print(f"PDF={OUTPUT_PDF}", flush=True)
    print(f"Total runtime={perf_counter() - total_started:.3f}s", flush=True)


if __name__ == "__main__":
    main()
