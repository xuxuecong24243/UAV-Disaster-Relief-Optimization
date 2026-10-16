"""根据问题1既有预处理结果和正式DEM绘制三维折线航迹与地形。

本程序只读取 ``results/segment_preprocessing.xlsx`` 和正式30米DEM，不运行
DEM预处理、组批或任何优化模型。每条去程航迹严格由四个关键点A-B-C-D构成：

    A：O01 作业海拔
    B：O01 平面位置处的计划巡航海拔
    C：服务区平面位置处的计划巡航海拔
    D：服务区作业海拔
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

import matplotlib

matplotlib.use("Agg")

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from pyproj import Transformer
import rasterio
from affine import Affine
from rasterio.enums import Resampling
from rasterio.windows import Window, from_bounds


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = ROOT
INPUT_FILE = input_file("problem1", "segment_preprocessing.xlsx")
DEM_FILE = dem_file()
OUTPUT_DIR = output_dir("problem1") / "figures_3d"
IMAGE_FILE = OUTPUT_DIR / "O01至各服务区去程三维航迹与地形.png"
README_FILE = OUTPUT_DIR / "三维航迹与地形绘图说明.txt"

DEPOT_ID = "O01"
SERVICE_IDS = [f"S{i:02d}" for i in range(1, 16)]
SOURCE_CRS = "EPSG:4326"  # WGS84 经纬度
PROJECTED_CRS = "EPSG:32649"  # WGS84 / UTM zone 49N
FIGURE_DPI = 300


def display_id(node_id: str) -> str:
    """将正式结果中的 S01-S15 显示为题目使用的 S001-S015。"""
    if node_id.startswith("S"):
        return f"S{int(node_id[1:]):03d}"
    return node_id


def configure_chinese_font() -> str:
    """选择当前系统中可用的中文字体。"""
    preferred = [
        "Microsoft YaHei",
        "SimHei",
        "Noto Sans CJK SC",
        "Source Han Sans SC",
        "Arial Unicode MS",
    ]
    installed = {font.name for font in fm.fontManager.ttflist}
    selected = next((name for name in preferred if name in installed), "DejaVu Sans")
    plt.rcParams["font.sans-serif"] = [selected, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    return selected


def load_formal_results() -> tuple[pd.DataFrame, pd.DataFrame]:
    """读取正式预处理工作簿中的节点表和 O01 出发航段表。"""
    if not INPUT_FILE.exists():
        raise FileNotFoundError(f"找不到问题1正式预处理结果：{INPUT_FILE}")

    nodes = pd.read_excel(require_file(INPUT_FILE), sheet_name="node_elevation")
    segments = pd.read_excel(require_file(INPUT_FILE), sheet_name="segment_detail")

    required_node_columns = {
        "node_id",
        "longitude",
        "latitude",
        "ground_elevation_m",
        "operation_altitude_m",
    }
    required_segment_columns = {
        "start_node",
        "end_node",
        "horizontal_distance_m",
        "max_ground_elevation_m",
        "cruise_altitude_m",
        "crossed_pixel_count",
        "forward_climb_height_m",
        "reverse_climb_height_m",
        "forward_descent_height_m",
        "reverse_descent_height_m",
    }
    missing_nodes = required_node_columns.difference(nodes.columns)
    missing_segments = required_segment_columns.difference(segments.columns)
    if missing_nodes:
        raise ValueError(f"node_elevation 缺少字段：{sorted(missing_nodes)}")
    if missing_segments:
        raise ValueError(f"segment_detail 缺少字段：{sorted(missing_segments)}")

    nodes = nodes[nodes["node_id"].isin([DEPOT_ID, *SERVICE_IDS])].copy()
    nodes = nodes.set_index("node_id").loc[[DEPOT_ID, *SERVICE_IDS]].reset_index()
    routes = segments[
        segments["start_node"].eq(DEPOT_ID)
        & segments["end_node"].isin(SERVICE_IDS)
    ].copy()
    routes = routes.set_index("end_node").loc[SERVICE_IDS].reset_index()

    if len(nodes) != 16 or len(routes) != 15:
        raise ValueError(
            f"节点或航段数量异常：nodes={len(nodes)}，O01 routes={len(routes)}"
        )
    return nodes, routes


def add_local_coordinates(nodes: pd.DataFrame) -> pd.DataFrame:
    """将 WGS84 经纬度投影至 UTM 49N，并将 O01 平移为局部原点。"""
    transformer = Transformer.from_crs(SOURCE_CRS, PROJECTED_CRS, always_xy=True)
    east, north = transformer.transform(
        nodes["longitude"].to_numpy(), nodes["latitude"].to_numpy()
    )
    result = nodes.copy()
    result["utm_easting_m"] = east
    result["utm_northing_m"] = north
    depot = result.loc[result["node_id"].eq(DEPOT_ID)].iloc[0]
    result["x_m"] = result["utm_easting_m"] - depot["utm_easting_m"]
    result["y_m"] = result["utm_northing_m"] - depot["utm_northing_m"]
    return result


def load_terrain(nodes: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """读取节点外围的正式DEM窗口，并转换为以O01为原点的局部坐标。"""
    if not DEM_FILE.exists():
        raise FileNotFoundError(f"找不到正式DEM：{DEM_FILE}")

    lon_min = float(nodes["longitude"].min()) - 0.008
    lon_max = float(nodes["longitude"].max()) + 0.008
    lat_min = float(nodes["latitude"].min()) - 0.008
    lat_max = float(nodes["latitude"].max()) + 0.008

    with rasterio.open(require_file(DEM_FILE)) as dataset:
        if dataset.crs is None or dataset.crs.to_epsg() != 4326:
            raise ValueError(f"DEM坐标系不是预期的EPSG:4326：{dataset.crs}")
        lon_min = max(lon_min, dataset.bounds.left)
        lon_max = min(lon_max, dataset.bounds.right)
        lat_min = max(lat_min, dataset.bounds.bottom)
        lat_max = min(lat_max, dataset.bounds.top)
        raw_window = from_bounds(lon_min, lat_min, lon_max, lat_max, dataset.transform)
        raw_window = raw_window.round_offsets().round_lengths()
        full_window = Window(0, 0, dataset.width, dataset.height)
        window = raw_window.intersection(full_window)

        # 限制曲面网格规模以保证论文图片清晰且绘制速度稳定。
        target_max_side = 190
        scale = max(window.height / target_max_side, window.width / target_max_side, 1.0)
        out_height = max(2, int(np.ceil(window.height / scale)))
        out_width = max(2, int(np.ceil(window.width / scale)))
        terrain = dataset.read(
            1,
            window=window,
            out_shape=(out_height, out_width),
            resampling=Resampling.bilinear,
            masked=True,
        ).astype(float)
        terrain = np.asarray(terrain.filled(np.nan), dtype=float)
        terrain[(terrain <= -30000.0) | ~np.isfinite(terrain)] = np.nan

        window_transform = dataset.window_transform(window)
        sampled_transform = window_transform * Affine.scale(
            window.width / out_width, window.height / out_height
        )

    rows, cols = np.meshgrid(
        np.arange(out_height, dtype=float),
        np.arange(out_width, dtype=float),
        indexing="ij",
    )
    longitude = (
        sampled_transform.c
        + (cols + 0.5) * sampled_transform.a
        + (rows + 0.5) * sampled_transform.b
    )
    latitude = (
        sampled_transform.f
        + (cols + 0.5) * sampled_transform.d
        + (rows + 0.5) * sampled_transform.e
    )

    transformer = Transformer.from_crs(SOURCE_CRS, PROJECTED_CRS, always_xy=True)
    east, north = transformer.transform(longitude, latitude)
    depot = nodes.loc[nodes["node_id"].eq(DEPOT_ID)].iloc[0]
    x = east - float(depot["utm_easting_m"])
    y = north - float(depot["utm_northing_m"])
    return np.asarray(x), np.asarray(y), terrain


def build_routes(nodes: pd.DataFrame, segments: pd.DataFrame) -> list[dict]:
    """直接用既有计划巡航海拔和节点作业海拔构造 A-B-C-D。"""
    node_map = nodes.set_index("node_id")
    depot = node_map.loc[DEPOT_ID]
    routes: list[dict] = []

    for segment in segments.itertuples(index=False):
        service = node_map.loc[segment.end_node]
        cruise = float(segment.cruise_altitude_m)
        points = np.array(
            [
                [depot.x_m, depot.y_m, depot.operation_altitude_m],
                [depot.x_m, depot.y_m, cruise],
                [service.x_m, service.y_m, cruise],
                [service.x_m, service.y_m, service.operation_altitude_m],
            ],
            dtype=float,
        )

        # 仅核验读取结果内部口径，不重新推导或覆盖任何预处理值。
        plotted_climb = points[1, 2] - points[0, 2]
        plotted_descent = points[2, 2] - points[3, 2]
        if not np.isclose(plotted_climb, segment.forward_climb_height_m, atol=1e-6):
            raise ValueError(f"{segment.end_node} 的去程爬升高度与预处理结果不一致")
        if not np.isclose(plotted_descent, segment.forward_descent_height_m, atol=1e-6):
            raise ValueError(f"{segment.end_node} 的去程下降高度与预处理结果不一致")

        routes.append(
            {
                "node_id": segment.end_node,
                "label": display_id(segment.end_node),
                "points": points,
                "horizontal_distance_m": float(segment.horizontal_distance_m),
                "max_ground_elevation_m": float(segment.max_ground_elevation_m),
                "cruise_altitude_m": cruise,
                "crossed_pixel_count": int(segment.crossed_pixel_count),
                "forward_climb_height_m": float(segment.forward_climb_height_m),
                "forward_descent_height_m": float(segment.forward_descent_height_m),
                "reverse_climb_height_m": float(segment.reverse_climb_height_m),
                "reverse_descent_height_m": float(segment.reverse_descent_height_m),
            }
        )
    return routes


def style_axis(ax, title: str) -> None:
    ax.set_xlabel("X/m", labelpad=10)
    ax.set_ylabel("Y/m", labelpad=10)
    ax.set_zlabel("Z/m", labelpad=8)
    ax.set_title(title, pad=18, fontsize=14)
    ax.grid(True, alpha=0.28)
    ax.view_init(elev=24, azim=-58)
    ax.tick_params(labelsize=9)


def set_visible_3d_aspect(ax, points: np.ndarray) -> None:
    """适度增强高程方向的可见性，同时保留坐标轴真实单位与刻度。"""
    spans = np.ptp(points, axis=0)
    spans = np.maximum(spans, 1.0)
    ax.set_box_aspect((spans[0], spans[1], max(spans[2] * 3.0, 0.25 * max(spans[:2]))))


def plot_all_routes(
    routes: list[dict], terrain_x: np.ndarray, terrain_y: np.ndarray, terrain_z: np.ndarray
) -> Path:
    fig = plt.figure(figsize=(13.2, 9.2))
    ax = fig.add_subplot(111, projection="3d")
    route_color = "#f28e2b"

    surface = ax.plot_surface(
        terrain_x,
        terrain_y,
        terrain_z,
        cmap="terrain",
        alpha=0.58,
        linewidth=0,
        antialiased=True,
        shade=True,
        zorder=0,
    )

    depot_point = routes[0]["points"][0]
    all_points = []
    for index, route in enumerate(routes):
        points = route["points"]
        all_points.append(points)
        ax.plot(
            points[:, 0],
            points[:, 1],
            points[:, 2],
            color=route_color,
            linewidth=1.25,
            label="去程航迹" if index == 0 else "_nolegend_",
            zorder=5,
        )
        service = points[3]
        ax.scatter(
            *service,
            color=route_color,
            s=30,
            edgecolors="white",
            linewidths=0.6,
        )
        ax.text(service[0], service[1], service[2] + 12, route["label"], fontsize=8)

    ax.scatter(
        *depot_point,
        marker="*",
        s=170,
        color="#c62828",
        edgecolors="white",
        linewidths=0.9,
        depthshade=False,
        zorder=10,
    )
    ax.text(
        depot_point[0] + 180,
        depot_point[1] - 180,
        depot_point[2] - 18,
        DEPOT_ID,
        fontsize=10,
        weight="bold",
    )
    style_axis(ax, "O01至各服务区单点往返任务去程三维航迹与地形")
    set_visible_3d_aspect(ax, np.vstack(all_points))
    colorbar = fig.colorbar(surface, ax=ax, shrink=0.62, pad=0.08, aspect=24)
    colorbar.set_label("地面海拔/m", fontsize=10)
    ax.legend(
        loc="upper left",
        bbox_to_anchor=(1.01, 0.98),
        fontsize=9,
        frameon=True,
    )
    fig.subplots_adjust(left=0.03, right=0.82, bottom=0.05, top=0.92)
    fig.savefig(assert_writable_output(IMAGE_FILE), dpi=FIGURE_DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return IMAGE_FILE


def write_readme(font_name: str, output: Path, terrain_shape: tuple[int, int]) -> None:
    lines = [
        "问题1三维飞行航迹示意图说明",
        "=" * 32,
        "",
        "1. 输入文件",
        f"   {INPUT_FILE}",
        "   读取工作表：node_elevation、segment_detail。",
        "   所有节点经纬度、作业海拔、计划巡航海拔及航段参数均直接取自该正式预处理结果。",
        f"   {DEM_FILE}",
        "   DEM仅用于绘制地形曲面，没有重新计算或覆盖任何预处理结果。",
        "   本程序未运行问题1组批或优化模型。",
        "",
        "2. 三维坐标构造",
        "   将WGS84经纬度（EPSG:4326）投影到WGS84/UTM 49N（EPSG:32649），",
        "   再减去O01的投影坐标，使O01的局部平面坐标为(0, 0)，x、y单位均为m；",
        "   z轴直接采用正式预处理结果中的海拔，单位为m。",
        f"   本图实际显示的DEM采样网格为{terrain_shape[0]}×{terrain_shape[1]}。",
        "",
        "3. 航迹关键点",
        "   A：O01平面位置、O01作业海拔；",
        "   B：O01平面位置、当前服务区对应的计划巡航海拔；",
        "   C：服务区平面位置、同一计划巡航海拔；",
        "   D：服务区平面位置、该服务区作业海拔。",
        "   去程航迹由折线A-B-C-D表示，即垂直爬升、水平巡航、垂直下降。",
        "",
        "4. 标识说明",
        "   正式结果中的服务区编号S01-S15在图中按S001-S015显示，与题目表述保持一致。",
        "   为清晰显示垂直飞行阶段，三维绘图区对Z方向作了视觉增强；坐标值和刻度仍为真实米制数据。",
        f"   绘图中文字体：{font_name}。",
        "",
        "5. 输出目录",
        f"   {OUTPUT_DIR}",
        "",
        "6. 输出文件（仅一张综合图）",
        f"   {output.name}",
    ]
    README_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def main() -> None:
    print("问题1三维航迹绘图开始")
    print(f"读取正式预处理结果：{INPUT_FILE}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    font_name = configure_chinese_font()
    nodes, segments = load_formal_results()
    nodes = add_local_coordinates(nodes)
    routes = build_routes(nodes, segments)
    terrain_x, terrain_y, terrain_z = load_terrain(nodes)
    print(f"已读取节点 {len(nodes)} 个、O01去程航段 {len(routes)} 条")
    print(f"坐标转换：{SOURCE_CRS} -> {PROJECTED_CRS}，并以O01为局部原点")
    print(f"读取正式DEM：{DEM_FILE}")
    print(f"地形绘图网格：{terrain_z.shape[0]} x {terrain_z.shape[1]}")

    output = plot_all_routes(routes, terrain_x, terrain_y, terrain_z)
    print(f"已生成综合图：{output}")
    write_readme(font_name, output, terrain_z.shape)
    print(f"已生成说明文件：{README_FILE}")
    print(f"完成：仅生成1张图片，输出目录为 {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
