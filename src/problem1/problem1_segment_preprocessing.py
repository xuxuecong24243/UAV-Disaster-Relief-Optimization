from pathlib import Path
import sys

# Keep historical module names while resolving all locations from this repository.
_RELEASE_ROOT = Path(__file__).resolve().parents[2]
if str(_RELEASE_ROOT) not in sys.path:
    sys.path.insert(0, str(_RELEASE_ROOT))
from project_paths import ROOT, bootstrap, raw_file, dem_file, input_file, output_dir, output_file, require_file, assert_writable_output

bootstrap()
import re

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.collections import PatchCollection
from matplotlib.patches import Patch, Polygon
from matplotlib.ticker import FormatStrFormatter, MaxNLocator
import numpy as np
import pandas as pd
from pyproj import Geod
import rasterio
from rasterio.features import rasterize
from rasterio.windows import Window, transform as window_transform


PROJECT_DIR = ROOT
NODE_FILE = raw_file('调度中心与服务区.xlsx')
DEM_FILE = dem_file()
RESULT_DIR = output_dir("problem1")
EXCEL_FILE = RESULT_DIR / "segment_preprocessing.xlsx"
CHECK_FIGURE_FILE = RESULT_DIR / "segment_O01_S01_dem_check.png"
NODE_ORDER = ["O01"] + [f"S{i:02d}" for i in range(1, 16)]
GEOD = Geod(ellps="WGS84")


def setup_chinese_font():
    """设置绘图中文字体。"""
    candidates = ["Microsoft YaHei", "SimHei", "SimSun", "Noto Sans CJK SC"]
    installed = {font.name for font in font_manager.fontManager.ttflist}
    for name in candidates:
        if name in installed:
            plt.rcParams["font.sans-serif"] = [name]
            break
    plt.rcParams["axes.unicode_minus"] = False


def normalize_node_id(value):
    """将附件中的 S001-S015 统一为 S01-S15。"""
    text = str(value).strip().upper()
    if re.fullmatch(r"O0*1", text):
        return "O01"
    match = re.fullmatch(r"S0*(\d+)", text)
    if match and 1 <= int(match.group(1)) <= 15:
        return f"S{int(match.group(1)):02d}"
    return None


def load_nodes(node_file):
    """从附件 Excel 中读取节点经纬度和地面海拔，并按统一顺序排列。"""
    raw = pd.read_excel(require_file(node_file), header=None)
    records = {}

    for _, row in raw.iterrows():
        node_id = normalize_node_id(row.iloc[0])
        if node_id is None:
            continue
        lon = pd.to_numeric(row.iloc[2], errors="coerce")
        lat = pd.to_numeric(row.iloc[3], errors="coerce")
        ground_elevation = pd.to_numeric(row.iloc[4], errors="coerce")
        if pd.isna(lon) or pd.isna(lat) or pd.isna(ground_elevation):
            raise ValueError(f"节点 {node_id} 的经纬度或附件海拔无法读取")
        records[node_id] = (
            float(lon),
            float(lat),
            float(ground_elevation),
        )

    missing = [node_id for node_id in NODE_ORDER if node_id not in records]
    if missing:
        raise ValueError(f"节点文件缺少以下节点: {', '.join(missing)}")

    nodes = pd.DataFrame(
        [(node_id, *records[node_id]) for node_id in NODE_ORDER],
        columns=["node_id", "longitude", "latitude", "ground_elevation_m"],
    )
    print("\n节点坐标与地面海拔（来自实际附件）:")
    print(nodes.to_string(index=False))
    return nodes


def load_dem(dem_file):
    """读取原始 DEM，并将元数据 NoData、-32767、NaN/Inf 等置为 NaN。"""
    with rasterio.open(require_file(dem_file)) as src:
        dem = src.read(1).astype(np.float64)
        metadata = {
            "shape": dem.shape,
            "crs": src.crs,
            "bounds": src.bounds,
            "transform": src.transform,
            "nodata": src.nodata,
        }

    minus_32767_mask = dem == -32767
    invalid_mask = ~np.isfinite(dem) | minus_32767_mask
    if metadata["nodata"] is not None:
        invalid_mask |= dem == metadata["nodata"]
    # 该地区高程不可能低于 -10000 m，此阈值用于排除其他明显的异常占位值。
    invalid_mask |= dem <= -10000
    dem[invalid_mask] = np.nan

    print("\nDEM 基本信息:")
    print(f"shape: {metadata['shape']}")
    print(f"CRS: {metadata['crs']}")
    print(f"bounds: {metadata['bounds']}")
    print(f"transform: {metadata['transform']!r}")
    print(f"nodata: {metadata['nodata']}")
    print(f"是否存在 -32767: {bool(np.any(minus_32767_mask))}")
    print(f"-32767 像元数量: {int(np.sum(minus_32767_mask))}")
    print(f"最小有效高程: {np.nanmin(dem):.6f} m")
    print(f"最大有效高程: {np.nanmax(dem):.6f} m")
    return dem, metadata


def calculate_node_operation_altitudes(nodes):
    """以附件地面海拔为基准计算 O01 和服务区的作业海拔。"""
    nodes = nodes.copy()
    invalid_nodes = nodes.loc[
        ~np.isfinite(nodes["ground_elevation_m"]), "node_id"
    ].tolist()
    if invalid_nodes:
        raise ValueError(
            "以下节点的附件地面海拔为无效值: " + ", ".join(invalid_nodes)
        )

    service_offset = np.where(nodes["node_id"].eq("O01"), 0.0, 30.0)
    nodes["operation_altitude_m"] = nodes["ground_elevation_m"] + service_offset

    print("\n节点附件地面海拔与作业海拔:")
    print(
        nodes[
            [
                "node_id",
                "longitude",
                "latitude",
                "ground_elevation_m",
                "operation_altitude_m",
            ]
        ].to_string(index=False)
    )
    return nodes


def calculate_geodesic_distance(lon1, lat1, lon2, lat2):
    """计算两节点间的 WGS84 椭球测地距离，单位为 m。"""
    _, _, distance = GEOD.inv(lon1, lat1, lon2, lat2)
    return float(distance)


def get_crossed_dem_cells(lon1, lat1, lon2, lat2, transform, dem_shape):
    """
    返回原始经纬度直线经过或接触的所有 DEM (row, col) 像元。

    先用逆仿射变换确定一个仅用于加速的局部栅格窗口，再在该窗口内
    rasterize 原始经纬度 LineString。all_touched=True 表示凡被线段穿过
    或接触的像元均计入；端点不会先取整到像元中心，因此不改变航段位置。
    """
    inverse = ~transform
    col1, row1 = inverse * (lon1, lat1)
    col2, row2 = inverse * (lon2, lat2)
    dem_height, dem_width = dem_shape

    # 外扩一个像元，避免恰好接触窗口边缘的像元被裁掉。
    row_start = max(0, int(np.floor(min(row1, row2))) - 1)
    row_stop = min(dem_height, int(np.floor(max(row1, row2))) + 2)
    col_start = max(0, int(np.floor(min(col1, col2))) - 1)
    col_stop = min(dem_width, int(np.floor(max(col1, col2))) + 2)

    if row_start >= row_stop or col_start >= col_stop:
        return np.empty((0, 2), dtype=int)

    window = Window(
        col_off=col_start,
        row_off=row_start,
        width=col_stop - col_start,
        height=row_stop - row_start,
    )
    local_transform = window_transform(window, transform)
    line = {
        "type": "LineString",
        "coordinates": [(lon1, lat1), (lon2, lat2)],
    }
    touched = rasterize(
        [(line, 1)],
        out_shape=(int(window.height), int(window.width)),
        transform=local_transform,
        fill=0,
        all_touched=True,
        dtype=np.uint8,
    )

    local_rows, local_cols = np.nonzero(touched)
    return np.column_stack(
        (local_rows + row_start, local_cols + col_start)
    ).astype(int)


def calculate_segment_max_elevation(dem, crossed_cells):
    """计算航段所经过有效 DEM 像元的最高地面高程。"""
    if len(crossed_cells) == 0:
        raise ValueError("航段未与任何 DEM 像元相交")
    elevations = dem[crossed_cells[:, 0], crossed_cells[:, 1]]
    valid_elevations = elevations[np.isfinite(elevations)]
    if len(valid_elevations) == 0:
        raise ValueError("航段经过的 DEM 像元均为无效值")
    return float(np.max(valid_elevations))


def check_nodes_in_bounds(nodes, bounds):
    """返回位于 DEM 地理范围之外的节点编号。"""
    inside = (
        nodes["longitude"].between(bounds.left, bounds.right, inclusive="both")
        & nodes["latitude"].between(bounds.bottom, bounds.top, inclusive="both")
    )
    return nodes.loc[~inside, "node_id"].tolist()


def calculate_all_segments(nodes, dem, metadata):
    """计算 16 个节点之间的三个对称矩阵和 120 条无重复航段。"""
    node_count = len(nodes)
    distance_matrix = np.zeros((node_count, node_count), dtype=float)
    max_ground_matrix = np.zeros((node_count, node_count), dtype=float)
    cruise_altitude_matrix = np.zeros((node_count, node_count), dtype=float)
    details = []
    crossed_cell_map = {}

    for i in range(node_count):
        start = nodes.iloc[i]
        for j in range(i + 1, node_count):
            end = nodes.iloc[j]
            distance = calculate_geodesic_distance(
                start.longitude, start.latitude, end.longitude, end.latitude
            )
            cells = get_crossed_dem_cells(
                start.longitude,
                start.latitude,
                end.longitude,
                end.latitude,
                metadata["transform"],
                metadata["shape"],
            )
            max_ground = calculate_segment_max_elevation(dem, cells)
            cruise_altitude = max_ground + 50.0

            distance_matrix[i, j] = distance_matrix[j, i] = distance
            max_ground_matrix[i, j] = max_ground_matrix[j, i] = max_ground
            cruise_altitude_matrix[i, j] = cruise_altitude_matrix[j, i] = cruise_altitude
            crossed_cell_map[(start.node_id, end.node_id)] = cells

            details.append(
                {
                    "start_node": start.node_id,
                    "end_node": end.node_id,
                    "start_lon": start.longitude,
                    "start_lat": start.latitude,
                    "end_lon": end.longitude,
                    "end_lat": end.latitude,
                    "horizontal_distance_m": distance,
                    "max_ground_elevation_m": max_ground,
                    "cruise_altitude_m": cruise_altitude,
                    "crossed_pixel_count": len(cells),
                }
            )

    return (
        distance_matrix,
        max_ground_matrix,
        cruise_altitude_matrix,
        pd.DataFrame(details),
        crossed_cell_map,
    )


def calculate_climb_height_matrix(nodes, cruise_altitude_matrix):
    """按“巡航海拔减起点作业海拔”计算有方向的爬升高度矩阵。"""
    operation_altitudes = nodes["operation_altitude_m"].to_numpy(dtype=float)
    climb_height_matrix = cruise_altitude_matrix - operation_altitudes[:, np.newaxis]
    np.fill_diagonal(climb_height_matrix, 0.0)
    return climb_height_matrix


def calculate_descent_height_matrix(nodes, cruise_altitude_matrix):
    """按“巡航海拔减终点作业海拔”计算有方向的下降高度矩阵。"""
    operation_altitudes = nodes["operation_altitude_m"].to_numpy(dtype=float)
    descent_height_matrix = (
        cruise_altitude_matrix - operation_altitudes[np.newaxis, :]
    )
    np.fill_diagonal(descent_height_matrix, 0.0)
    return descent_height_matrix


def add_climb_heights_to_segment_detail(
    segment_detail, nodes, climb_height_matrix
):
    """为无重复航段明细补充正向和反向爬升高度。"""
    node_index = {node_id: i for i, node_id in enumerate(nodes["node_id"])}
    segment_detail = segment_detail.copy()
    segment_detail["forward_climb_height_m"] = [
        climb_height_matrix[node_index[start], node_index[end]]
        for start, end in zip(
            segment_detail["start_node"], segment_detail["end_node"]
        )
    ]
    segment_detail["reverse_climb_height_m"] = [
        climb_height_matrix[node_index[end], node_index[start]]
        for start, end in zip(
            segment_detail["start_node"], segment_detail["end_node"]
        )
    ]
    return segment_detail


def add_descent_heights_to_segment_detail(
    segment_detail, nodes, descent_height_matrix
):
    """为无重复航段明细补充正向和反向下降高度。"""
    node_index = {node_id: i for i, node_id in enumerate(nodes["node_id"])}
    segment_detail = segment_detail.copy()
    segment_detail["forward_descent_height_m"] = [
        descent_height_matrix[node_index[start], node_index[end]]
        for start, end in zip(
            segment_detail["start_node"], segment_detail["end_node"]
        )
    ]
    segment_detail["reverse_descent_height_m"] = [
        descent_height_matrix[node_index[end], node_index[start]]
        for start, end in zip(
            segment_detail["start_node"], segment_detail["end_node"]
        )
    ]
    return segment_detail


def validate_results(
    nodes,
    metadata,
    distance_matrix,
    max_ground_matrix,
    cruise_altitude_matrix,
    climb_height_matrix,
    descent_height_matrix,
    segment_detail,
):
    """执行题目要求的完整性、一致性与边界检查。"""
    outside_nodes = check_nodes_in_bounds(nodes, metadata["bounds"])
    off_diagonal = ~np.eye(len(nodes), dtype=bool)
    empty_segments = segment_detail.loc[
        segment_detail["crossed_pixel_count"] <= 0, ["start_node", "end_node"]
    ]
    ground_elevations = nodes["ground_elevation_m"].to_numpy(dtype=float)
    operation_altitudes = nodes["operation_altitude_m"].to_numpy(dtype=float)
    o01_mask = nodes["node_id"].eq("O01").to_numpy()
    service_mask = ~o01_mask
    negative_positions = np.argwhere(
        off_diagonal & (climb_height_matrix < 0)
    )
    negative_descent_positions = np.argwhere(
        off_diagonal & (descent_height_matrix < 0)
    )

    checks = {
        "descent_height_matrix 对角线为0": np.allclose(
            np.diag(descent_height_matrix), 0
        ),
        "所有非对角线下降高度均不小于0": len(negative_descent_positions) == 0,
        "下降高度等于巡航海拔减终点作业海拔": np.allclose(
            descent_height_matrix[off_diagonal],
            (
                cruise_altitude_matrix
                - operation_altitudes[np.newaxis, :]
            )[off_diagonal],
        ),
        "所有非对角线下降高度等于反向爬升高度": np.allclose(
            descent_height_matrix[off_diagonal],
            climb_height_matrix.T[off_diagonal],
        ),
        "节点数量为16": len(nodes) == 16,
        "不同航段数量为120": len(segment_detail) == 120,
        "distance_matrix 对称": np.allclose(distance_matrix, distance_matrix.T),
        "max_ground_elevation_matrix 对称": np.allclose(
            max_ground_matrix, max_ground_matrix.T
        ),
        "cruise_altitude_matrix 对称": np.allclose(
            cruise_altitude_matrix, cruise_altitude_matrix.T
        ),
        "distance_matrix 对角线为0": np.allclose(np.diag(distance_matrix), 0),
        "max_ground_elevation_matrix 对角线为0": np.allclose(
            np.diag(max_ground_matrix), 0
        ),
        "cruise_altitude_matrix 对角线为0": np.allclose(
            np.diag(cruise_altitude_matrix), 0
        ),
        "非对角线巡航海拔=最高地面高程+50m": np.allclose(
            cruise_altitude_matrix[off_diagonal],
            max_ground_matrix[off_diagonal] + 50.0,
        ),
        "不存在无有效DEM像元的航段": not segment_detail[
            "max_ground_elevation_m"
        ].isna().any(),
        "不存在DEM范围外节点": len(outside_nodes) == 0,
        "每条航段经过像元数量均大于0": empty_segments.empty,
        "节点地面高程均为有效值": np.all(np.isfinite(ground_elevations)),
        "O01作业海拔等于地面高程": np.allclose(
            operation_altitudes[o01_mask], ground_elevations[o01_mask]
        ),
        "S01-S15作业海拔等于地面高程加30m": np.allclose(
            operation_altitudes[service_mask],
            ground_elevations[service_mask] + 30.0,
        ),
        "climb_height_matrix 对角线为0": np.allclose(
            np.diag(climb_height_matrix), 0
        ),
        "所有非对角爬升高度均不小于0": len(negative_positions) == 0,
        "方向性爬升高度等于巡航海拔减起点作业海拔": np.allclose(
            climb_height_matrix[off_diagonal],
            (
                cruise_altitude_matrix
                - operation_altitudes[:, np.newaxis]
            )[off_diagonal],
        ),
    }

    print("\n结果自动检查:")
    for description, passed in checks.items():
        print(f"[{'通过' if passed else '失败'}] {description}")

    failures = [description for description, passed in checks.items() if not passed]
    if outside_nodes:
        print(f"DEM 范围外节点: {', '.join(outside_nodes)}")
    if not empty_segments.empty:
        print("无像元航段:")
        print(empty_segments.to_string(index=False))
    if len(negative_positions) > 0:
        negative_details = []
        for start_index, end_index in negative_positions:
            negative_details.append(
                {
                    "start_node": nodes.iloc[start_index].node_id,
                    "end_node": nodes.iloc[end_index].node_id,
                    "cruise_altitude": cruise_altitude_matrix[
                        start_index, end_index
                    ],
                    "start_operation_altitude": operation_altitudes[start_index],
                    "calculated_climb_height": climb_height_matrix[
                        start_index, end_index
                    ],
                }
            )
        print("发现负爬升高度航段:")
        print(pd.DataFrame(negative_details).to_string(index=False))
    if failures:
        raise RuntimeError("结果检查失败: " + "; ".join(failures))


def save_results(
    output_file,
    nodes,
    distance_matrix,
    max_ground_matrix,
    cruise_altitude_matrix,
    climb_height_matrix,
    descent_height_matrix,
    segment_detail,
):
    """将节点高程、五个矩阵和航段长表保存为 Excel。"""
    output_file.parent.mkdir(parents=True, exist_ok=True)
    labels = nodes["node_id"].tolist()

    matrices = {
        "distance_matrix": distance_matrix,
        "max_ground_elevation_matrix": max_ground_matrix,
        "cruise_altitude_matrix": cruise_altitude_matrix,
        "climb_height_matrix": climb_height_matrix,
        "descent_height_matrix": descent_height_matrix,
    }
    with pd.ExcelWriter(assert_writable_output(output_file), engine="openpyxl") as writer:
        for sheet_name, matrix in matrices.items():
            frame = pd.DataFrame(matrix, index=labels, columns=labels)
            frame.index.name = "node"
            frame.to_excel(writer, sheet_name=sheet_name)
        nodes[
            [
                "node_id",
                "longitude",
                "latitude",
                "ground_elevation_m",
                "operation_altitude_m",
            ]
        ].to_excel(writer, sheet_name="node_elevation", index=False)
        segment_detail.to_excel(writer, sheet_name="segment_detail", index=False)

        for worksheet in writer.book.worksheets:
            worksheet.freeze_panes = (
                "B2" if worksheet.title in matrices else "A2"
            )
            worksheet.column_dimensions["A"].width = 15
            for column_cells in worksheet.iter_cols(min_col=2):
                worksheet.column_dimensions[column_cells[0].column_letter].width = 18
                for cell in column_cells[1:]:
                    if isinstance(cell.value, float):
                        cell.number_format = "0.000000"


def pixel_polygon(row, col, transform):
    """返回指定 DEM 像元四角的地理坐标。"""
    return [
        transform * (col, row),
        transform * (col + 1, row),
        transform * (col + 1, row + 1),
        transform * (col, row + 1),
    ]


def plot_segment_check(nodes, dem, metadata, crossed_cells, output_file):
    """绘制 O01-S01 原始直线与 all_touched 像元的放大检查图。"""
    setup_chinese_font()
    output_file.parent.mkdir(parents=True, exist_ok=True)
    start = nodes.loc[nodes["node_id"] == "O01"].iloc[0]
    end = nodes.loc[nodes["node_id"] == "S01"].iloc[0]
    bounds = metadata["bounds"]
    extent = [bounds.left, bounds.right, bounds.bottom, bounds.top]

    fig, ax = plt.subplots(figsize=(8.6, 8.0), dpi=300)
    image = ax.imshow(
        dem,
        extent=extent,
        origin="upper",
        cmap="terrain",
        vmin=np.nanmin(dem),
        vmax=np.nanmax(dem),
        interpolation="nearest",
    )

    patches = [
        Polygon(pixel_polygon(row, col, metadata["transform"]), closed=True)
        for row, col in crossed_cells
    ]
    collection = PatchCollection(
        patches,
        facecolor="#ff2d55",
        edgecolor="#8b0000",
        linewidth=0.35,
        alpha=0.52,
        zorder=3,
    )
    ax.add_collection(collection)
    ax.plot(
        [start.longitude, end.longitude],
        [start.latitude, end.latitude],
        color="white",
        linewidth=1.1,
        label="O01-S01 原始经纬度直线",
        zorder=4,
    )
    ax.scatter(
        start.longitude,
        start.latitude,
        marker="*",
        s=130,
        color="#d7191c",
        edgecolor="white",
        linewidth=0.8,
        label="O01",
        zorder=5,
    )
    ax.scatter(
        end.longitude,
        end.latitude,
        marker="o",
        s=55,
        color="#0072b2",
        edgecolor="white",
        linewidth=0.8,
        label="S01",
        zorder=5,
    )

    lon_padding = max(abs(end.longitude - start.longitude) * 0.18, 0.002)
    lat_padding = max(abs(end.latitude - start.latitude) * 0.10, 0.002)
    ax.set_xlim(
        min(start.longitude, end.longitude) - lon_padding,
        max(start.longitude, end.longitude) + lon_padding,
    )
    ax.set_ylim(
        min(start.latitude, end.latitude) - lat_padding,
        max(start.latitude, end.latitude) + lat_padding,
    )
    ax.set_aspect("equal")
    ax.ticklabel_format(axis="x", style="plain", useOffset=False)
    ax.xaxis.set_major_locator(MaxNLocator(nbins=5))
    ax.xaxis.set_major_formatter(FormatStrFormatter("%.3f"))
    ax.set_xlabel("经度（°E）")
    ax.set_ylabel("纬度（°N）")
    ax.set_title("O01-S01 航段与 DEM 像元映射检查")
    ax.grid(color="white", linewidth=0.5, alpha=0.35)
    handles, labels = ax.get_legend_handles_labels()
    cell_handle = Patch(
        facecolor="#ff2d55",
        edgecolor="#8b0000",
        alpha=0.52,
        label="all_touched 航段像元",
    )
    ax.legend(
        [cell_handle, *handles],
        ["all_touched 航段像元", *labels],
        loc="upper left",
        fontsize=8.5,
        framealpha=0.92,
    )
    colorbar = fig.colorbar(image, ax=ax, shrink=0.82, pad=0.03)
    colorbar.set_label("高程（m）")
    fig.tight_layout()
    fig.savefig(assert_writable_output(output_file), bbox_inches="tight")
    plt.close(fig)


def main():
    print(f"节点坐标文件: {NODE_FILE}")
    print(f"DEM 文件: {DEM_FILE}")
    nodes = load_nodes(NODE_FILE)
    dem, metadata = load_dem(DEM_FILE)

    outside_nodes = check_nodes_in_bounds(nodes, metadata["bounds"])
    if outside_nodes:
        raise ValueError("以下节点位于 DEM bounds 外: " + ", ".join(outside_nodes))
    nodes = calculate_node_operation_altitudes(nodes)

    (
        distance_matrix,
        max_ground_matrix,
        cruise_altitude_matrix,
        segment_detail,
        crossed_cell_map,
    ) = calculate_all_segments(nodes, dem, metadata)
    climb_height_matrix = calculate_climb_height_matrix(
        nodes, cruise_altitude_matrix
    )
    descent_height_matrix = calculate_descent_height_matrix(
        nodes, cruise_altitude_matrix
    )
    segment_detail = add_climb_heights_to_segment_detail(
        segment_detail, nodes, climb_height_matrix
    )
    segment_detail = add_descent_heights_to_segment_detail(
        segment_detail, nodes, descent_height_matrix
    )

    validate_results(
        nodes,
        metadata,
        distance_matrix,
        max_ground_matrix,
        cruise_altitude_matrix,
        climb_height_matrix,
        descent_height_matrix,
        segment_detail,
    )
    save_results(
        EXCEL_FILE,
        nodes,
        distance_matrix,
        max_ground_matrix,
        cruise_altitude_matrix,
        climb_height_matrix,
        descent_height_matrix,
        segment_detail,
    )
    plot_segment_check(
        nodes,
        dem,
        metadata,
        crossed_cell_map[("O01", "S01")],
        CHECK_FIGURE_FILE,
    )

    o01_s01 = segment_detail.query(
        "start_node == 'O01' and end_node == 'S01'"
    ).iloc[0]
    o01 = nodes.loc[nodes["node_id"] == "O01"].iloc[0]
    s01 = nodes.loc[nodes["node_id"] == "S01"].iloc[0]
    o01_index = NODE_ORDER.index("O01")
    s01_index = NODE_ORDER.index("S01")

    print("\n重点节点高程:")
    print(f"O01 附件地面海拔: {o01.ground_elevation_m:.6f} m")
    print(f"O01 作业海拔: {o01.operation_altitude_m:.6f} m")
    print(f"S01 附件地面海拔: {s01.ground_elevation_m:.6f} m")
    print(f"S01 作业海拔: {s01.operation_altitude_m:.6f} m")
    print("\nO01-S01 航段结果:")
    print(f"最高地面高程: {o01_s01.max_ground_elevation_m:.6f} m")
    print(f"计划巡航海拔: {o01_s01.cruise_altitude_m:.6f} m")
    print(
        "O01 → S01 爬升高度: "
        f"{climb_height_matrix[o01_index, s01_index]:.6f} m "
        f"= {o01_s01.cruise_altitude_m:.6f} - {o01.operation_altitude_m:.6f}"
    )
    print(
        "S01 → O01 爬升高度: "
        f"{climb_height_matrix[s01_index, o01_index]:.6f} m "
        f"= {o01_s01.cruise_altitude_m:.6f} - {s01.operation_altitude_m:.6f}"
    )
    print(f"\nExcel 已保存: {EXCEL_FILE}")
    print(f"检查图已保存: {CHECK_FIGURE_FILE}")


if __name__ == "__main__":
    main()
