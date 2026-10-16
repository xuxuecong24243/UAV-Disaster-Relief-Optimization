from pathlib import Path
import sys

# Keep historical module names while resolving all locations from this repository.
_RELEASE_ROOT = Path(__file__).resolve().parents[2]
if str(_RELEASE_ROOT) not in sys.path:
    sys.path.insert(0, str(_RELEASE_ROOT))
from project_paths import ROOT, bootstrap, raw_file, dem_file, input_file, output_dir, output_file, require_file, assert_writable_output

bootstrap()

import pandas as pd
import rasterio


# =========================
# 1. 文件路径
# =========================

BASE_DIR = ROOT

dem_path = dem_file()

output_path = output_file("utilities", 'DEM数据.xlsx')


# =========================
# 2. 读取 DEM
# =========================

def main():
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(require_file(dem_path)) as src:

        # 读取第一个波段，即地面高程矩阵
        dem = src.read(1)

        print("DEM矩阵大小：", dem.shape)
        print("坐标系：", src.crs)
        print("地理范围：", src.bounds)

        data = []

        # =========================
        # 3. 每隔10个像元抽取一个点
        # =========================

        step = 10

        for row in range(0, src.height, step):

            for col in range(0, src.width, step):

                # 获取像元中心的经纬度
                lon, lat = src.xy(row, col)

                # 获取该像元的地面高程
                elevation = dem[row, col]

                data.append([
                    row,
                    col,
                    lon,
                    lat,
                    elevation
                ])


    # =========================
    # 4. 转换为 DataFrame
    # =========================

    df = pd.DataFrame(
        data,
        columns=[
            "行号",
            "列号",
            "经度",
            "纬度",
            "地面高程(m)"
        ]
    )


    # =========================
    # 5. 保存 Excel
    # =========================

    df.to_excel(assert_writable_output(output_path), index=False)


    # =========================
    # 6. 输出基本信息
    # =========================

    print()
    print("原始DEM像元数量：", dem.size)
    print("抽样间隔：每", step, "个像元")
    print("导出数据数量：", len(df))
    print("最低高程：", df["地面高程(m)"].min(), "m")
    print("最高高程：", df["地面高程(m)"].max(), "m")
    print()
    print("Excel已保存到：")
    print(output_path)

if __name__ == "__main__":
    main()
