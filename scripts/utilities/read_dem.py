from pathlib import Path
import sys

# Keep historical module names while resolving all locations from this repository.
_RELEASE_ROOT = Path(__file__).resolve().parents[2]
if str(_RELEASE_ROOT) not in sys.path:
    sys.path.insert(0, str(_RELEASE_ROOT))
from project_paths import ROOT, bootstrap, raw_file, dem_file, input_file, output_dir, output_file, require_file, assert_writable_output

bootstrap()

import numpy as np
import rasterio


# 项目根目录
BASE_DIR = ROOT

# DEM 文件路径
dem_path = dem_file()


def main():
    with rasterio.open(require_file(dem_path)) as src:
        # 读取第一波段，即 DEM 高程矩阵
        dem = src.read(1)

        print("DEM 文件：", dem_path)
        print("DEM 矩阵大小：", dem.shape)
        print("坐标系：", src.crs)
        print("地理范围：", src.bounds)
        print("仿射变换：", src.transform)
        print("NoData 值：", src.nodata)

        # 去除 NoData 后统计有效高程
        if src.nodata is not None:
            valid_dem = np.where(dem == src.nodata, np.nan, dem)
        else:
            valid_dem = dem.astype(float)

        print("最低高程：", np.nanmin(valid_dem), "m")
        print("最高高程：", np.nanmax(valid_dem), "m")
        print("平均高程：", np.nanmean(valid_dem), "m")

        # ==============================
        # 查找最高高程的位置
        # ==============================
        flat_index = np.nanargmax(valid_dem)

        row, col = np.unravel_index(
            flat_index,
            valid_dem.shape
        )

        max_elevation = valid_dem[row, col]

        # 获取该像元中心的经纬度
        lon, lat = src.xy(row, col)

        print("\n最高高程信息：")
        print("最高高程：", max_elevation, "m")
        print("Python 行索引 row：", row)
        print("Python 列索引 col：", col)
        print("经度：", lon)
        print("纬度：", lat)

        # 如果需要和 Excel 对照
        print("Excel 行号（不考虑表头）：", row + 1)
        print("Excel 列号：", col + 1)

if __name__ == "__main__":
    main()
