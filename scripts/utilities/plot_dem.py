from pathlib import Path
import sys

# Keep historical module names while resolving all locations from this repository.
_RELEASE_ROOT = Path(__file__).resolve().parents[2]
if str(_RELEASE_ROOT) not in sys.path:
    sys.path.insert(0, str(_RELEASE_ROOT))
from project_paths import ROOT, bootstrap, raw_file, dem_file, input_file, output_dir, output_file, require_file, assert_writable_output

bootstrap()

import numpy as np
import pandas as pd
import rasterio

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager


BASE_DIR = ROOT
DEM_PATH = dem_file()
NODE_PATH = raw_file('调度中心与服务区.xlsx')
OUT_2D = output_file("utilities", 'dem_2d.png')
OUT_3D = output_file("utilities", 'dem_3d.png')


def setup_chinese_font():
    """设置中文字体，避免论文图中的中文标题和图例乱码。"""
    candidates = ["Microsoft YaHei", "SimHei", "SimSun", "Noto Sans CJK SC"]
    installed = {font.name for font in font_manager.fontManager.ttflist}
    for name in candidates:
        if name in installed:
            plt.rcParams["font.sans-serif"] = [name]
            break
    plt.rcParams["axes.unicode_minus"] = False


def read_dem():
    """读取原始 30 米 DEM，并处理 NoData 与明显无效高程值。"""
    with rasterio.open(require_file(DEM_PATH)) as src:
        dem = src.read(1).astype(float)
        bounds = src.bounds
        transform = src.transform
        crs = src.crs
        nodata = src.nodata

    invalid_mask = ~np.isfinite(dem)
    if nodata is not None:
        invalid_mask |= dem == nodata

    minus_32767_mask = dem == -32767
    obvious_invalid_mask = dem <= -10000
    invalid_mask |= minus_32767_mask | obvious_invalid_mask
    dem[invalid_mask] = np.nan

    print(f"DEM 文件: {DEM_PATH}")
    print(f"坐标系: {crs}")
    print(f"地理范围: {bounds}")
    print(f"仿射变换: {transform}")
    print(f"GeoTIFF 元数据 src.nodata: {nodata}")
    print(f"DEM 中是否存在 -32767: {bool(np.any(minus_32767_mask))}")
    print(f"-32767 像元数量: {int(np.sum(minus_32767_mask))}")
    print(f"处理后的有效高程最小值: {np.nanmin(dem):.6f} m")
    print(f"处理后的有效高程最大值: {np.nanmax(dem):.6f} m")
    print(f"处理后的有效高程平均值: {np.nanmean(dem):.6f} m")

    return dem, bounds, transform


def read_nodes():
    """读取 O01 调度中心与 S01-S15 服务区经纬度。"""
    if not NODE_PATH.exists():
        return None

    raw = pd.read_excel(require_file(NODE_PATH), header=None)
    nodes = []
    for _, row in raw.iterrows():
        node_id = row.iloc[0]
        lon = row.iloc[2]
        lat = row.iloc[3]
        if pd.isna(node_id) or pd.isna(lon) or pd.isna(lat):
            continue

        node_id = str(node_id).strip()
        if node_id == "O01" or node_id.startswith("S"):
            label = node_id
            if label.startswith("S") and len(label) == 4 and label[1:].isdigit():
                label = f"S{int(label[1:]):02d}"
            nodes.append({"id": label, "lon": float(lon), "lat": float(lat)})

    return pd.DataFrame(nodes)


def plot_2d(dem, bounds, nodes):
    """绘制二维 DEM 高程图，并叠加调度中心和服务区。"""
    fig, ax = plt.subplots(figsize=(9.2, 7.2), dpi=300)
    extent = [bounds.left, bounds.right, bounds.bottom, bounds.top]
    vmin, vmax = np.nanmin(dem), np.nanmax(dem)

    image = ax.imshow(
        dem,
        extent=extent,
        origin="upper",
        cmap="terrain",
        vmin=vmin,
        vmax=vmax,
        interpolation="bilinear",
        aspect="auto",
    )
    cbar = fig.colorbar(image, ax=ax, shrink=0.88, pad=0.02)
    cbar.set_label("高程（m）", fontsize=11)

    if nodes is not None and not nodes.empty:
        center = nodes[nodes["id"] == "O01"]
        services = nodes[nodes["id"] != "O01"]

        if not services.empty:
            ax.scatter(
                services["lon"],
                services["lat"],
                s=32,
                marker="o",
                c="#1f78b4",
                edgecolors="white",
                linewidths=0.8,
                label="服务区 S01-S15",
                zorder=4,
            )
        if not center.empty:
            ax.scatter(
                center["lon"],
                center["lat"],
                s=90,
                marker="*",
                c="#d7191c",
                edgecolors="white",
                linewidths=0.8,
                label="调度中心 O01",
                zorder=5,
            )

        for i, row in nodes.reset_index(drop=True).iterrows():
            dx = 0.0025 if i % 2 == 0 else -0.006
            dy = 0.0022 if i % 3 != 0 else -0.0038
            ax.annotate(
                row["id"],
                xy=(row["lon"], row["lat"]),
                xytext=(row["lon"] + dx, row["lat"] + dy),
                fontsize=8,
                color="black",
                bbox={"boxstyle": "round,pad=0.16", "fc": "white", "ec": "none", "alpha": 0.72},
                arrowprops={"arrowstyle": "-", "lw": 0.45, "color": "0.25", "alpha": 0.7},
                zorder=6,
            )

        ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0), frameon=True, fontsize=9)

    # ax.set_title("镇龙乡及周边30米DEM", fontsize=15, pad=12)
    ax.set_xlabel("经度（°E）", fontsize=11)
    ax.set_ylabel("纬度（°N）", fontsize=11)
    ax.set_xlim(bounds.left, bounds.right)
    ax.set_ylim(bounds.bottom, bounds.top)
    ax.grid(color="white", alpha=0.25, linewidth=0.5)
    fig.tight_layout()
    fig.savefig(assert_writable_output(OUT_2D), bbox_inches="tight")
    plt.close(fig)


def plot_3d(dem, bounds, transform):
    """降采样绘制三维 DEM，仅用于可视化观察，不改变原始 DEM 计算数据。"""
    from mpl_toolkits.mplot3d import Axes3D  # noqa: F401

    max_points = 240
    step = max(1, int(np.ceil(max(dem.shape) / max_points)))
    dem_sample = dem[::step, ::step]
    rows = np.arange(0, dem.shape[0], step)
    cols = np.arange(0, dem.shape[1], step)

    lon = transform.c + (cols + 0.5) * transform.a
    lat = transform.f + (rows + 0.5) * transform.e
    x, y = np.meshgrid(lon, lat)

    fig = plt.figure(figsize=(10, 7.4), dpi=300)
    ax = fig.add_subplot(111, projection="3d")
    surf = ax.plot_surface(
        x,
        y,
        dem_sample,
        cmap="terrain",
        linewidth=0,
        antialiased=True,
        rstride=1,
        cstride=1,
        vmin=np.nanmin(dem),
        vmax=np.nanmax(dem),
    )

    cbar = fig.colorbar(surf, ax=ax, shrink=0.62, pad=0.08)
    cbar.set_label("高程（m）", fontsize=10)
    # ax.set_title("镇龙乡及周边30米DEM三维地形图", fontsize=14, pad=14)
    ax.set_xlabel("经度（°E）", labelpad=8)
    ax.set_ylabel("纬度（°N）", labelpad=8)
    ax.set_zlabel("高程（m）", labelpad=8)
    ax.set_xlim(bounds.left, bounds.right)
    ax.set_ylim(bounds.bottom, bounds.top)
    ax.view_init(elev=36, azim=-126)
    ax.tick_params(labelsize=8)
    fig.tight_layout()
    fig.savefig(assert_writable_output(OUT_3D), bbox_inches="tight")
    plt.close(fig)


def main():
    OUT_2D.parent.mkdir(parents=True, exist_ok=True)
    setup_chinese_font()
    dem, bounds, transform = read_dem()
    nodes = read_nodes()
    plot_2d(dem, bounds, nodes)
    plot_3d(dem, bounds, transform)
    print(f"二维 DEM 图已保存: {OUT_2D}")
    print(f"三维 DEM 图已保存: {OUT_3D}")


if __name__ == "__main__":
    main()
