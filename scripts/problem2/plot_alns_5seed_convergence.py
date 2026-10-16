from pathlib import Path
import sys

# Keep historical module names while resolving all locations from this repository.
_RELEASE_ROOT = Path(__file__).resolve().parents[2]
if str(_RELEASE_ROOT) not in sys.path:
    sys.path.insert(0, str(_RELEASE_ROOT))
from project_paths import ROOT, bootstrap, raw_file, dem_file, input_file, output_dir, output_file, require_file, assert_writable_output

bootstrap()

import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


# ============================================================
# 1. 参数设置
# ============================================================

RESULT_DIR = output_dir("problem2") / 'alns_5seed_runs'

SEEDS = [
    20260924,
    20260925,
    20260926,
    20260927,
    20260928,
]

OUTPUT_PATH = RESULT_DIR / "问题2_ALNS_5种子收敛曲线.png"


# ============================================================
# 2. 中文字体设置
# ============================================================

def main():
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    plt.rcParams["font.sans-serif"] = [
        "Microsoft YaHei",
        "SimHei"
    ]
    plt.rcParams["axes.unicode_minus"] = False


    # ============================================================
    # 3. 绘制5个随机种子的收敛曲线
    # ============================================================

    fig, ax = plt.subplots(figsize=(12, 6.5))

    for seed in SEEDS:

        csv_path = input_file("problem2", f"alns_5seed_runs/seed_{seed}_trace.csv")

        df = pd.read_csv(require_file(csv_path))

        ax.plot(
            df["iteration"],
            df["best_F"],
            linewidth=1.6,
            label=str(seed),
        )


    # ============================================================
    # 4. 图形格式
    # ============================================================

    ax.set_xlabel("迭代次数", fontsize=12)
    ax.set_ylabel(r"全局最优目标值 $F$", fontsize=12)

    #ax.set_title(
    #   "不同随机种子下改进ALNS的收敛曲线",
    #   fontsize=15
    #)

    ax.legend(
        title="随机种子",
        ncol=2,
        fontsize=10,
        title_fontsize=10,
    )

    ax.grid(
        True,
        alpha=0.25,
    )

    ax.tick_params(
        axis="both",
        labelsize=10,
    )

    fig.tight_layout()


    # ============================================================
    # 5. 保存图片
    # ============================================================

    plt.savefig(
        assert_writable_output(OUTPUT_PATH),
        dpi=300,
        bbox_inches="tight",
    )

    plt.show()

    print(f"图片已保存至：{OUTPUT_PATH}")

if __name__ == "__main__":
    main()
