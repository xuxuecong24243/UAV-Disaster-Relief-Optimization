# 扩展实验与历史结果

本目录保存正式主流程之外的基线、参数分析、稳定性检查及备选算法。历史工作簿用于解释实验设计和追溯结论；Phase 3 只验证了工程入口、导入和部分数据读取，没有重新执行这些搜索。名称中出现 `final` 或 `formal` 不足以认定为当前正式版本，应以 [正式版本清单](../docs/formal_versions.json) 为准。

实验输出位于 `results/runs/<运行标识>/problemN/`。本目录的 `problemN/results/` 是只读历史归档，因衍生数据权限和文件元数据仍待审核，已被 Git 忽略；公开候选源码中不附带这些工作簿。来源与原始哈希见 [provenance.csv](../docs/provenance.csv)，公开审核见 [review_required.csv](../docs/review_required.csv)。

## 正式主流程边界

| 问题 | 当前正式入口或结果 | 与扩展实验的区别 |
|---|---|---|
| 问题一 | `src/problem1/` 的航段预处理、最大安全载荷、集合划分 | BFD 是对照基线，安全余量分析是多情景重算，均非集合划分的必经前置步骤 |
| 问题二 | [problem2_final_run_and_visualization.py](../src/problem2/problem2_final_run_and_visualization.py) | 1000 次迭代、种子 20260927、权重 `(0.55, 0.15, 0.15, 0.15)`；不等于本目录的 1500 次包装器 |
| 问题三 | [problem3_vns.py](../src/problem3/problem3_vns.py)，正式归档 `problem3_vns_20cycles.xlsx` | VNS 从问题二正式结果恢复运输基因，不能解释为直接接续归档 ALNS 最优解 |
| 问题四 | [problem4_full_enumeration.py](../src/problem4/problem4_full_enumeration.py) 与最终方案报告 | 固定问题三时序的完整分区枚举和资源分析；当前没有单独的扩展实验目录 |

## 问题一：组批基线与返航余量

| 脚本 | 实验内容 | 历史归档与解释范围 |
|---|---|---|
| [problem1_bfd_batching.py](problem1/problem1_bfd_batching.py) | Best Fit Decreasing 组批基线，受载荷、体积和能源可行性约束 | `problem1/results/problem1_bfd_batching.xlsx`；启发式基线不提供集合划分的最优性证明 |
| [problem1_safety_margin_analysis.py](problem1/problem1_safety_margin_analysis.py) | 返航安全余量 `rho=0, 0.05, …, 0.30`；基准为 `0.20`，各情景重新计算载荷和组批 | `problem1/results/problem1_safety_margin_analysis.xlsx`；属于改变实验情景后的敏感性分析，不替换正式参数 |

`problem1/results/problem1_batch_candidates.xlsx` 保留候选批次证据。正式集合划分结果仍在 `results/reference/problem1/`，不得用实验输出覆盖。安全余量实验调用 Gurobi 多次求解，不属于轻量数据复核。

## 问题二：迭代深度、随机种子和备选算法

| 脚本 | 分类与配置 | 归档或限制 |
|---|---|---|
| [problem2_alns_iteration_convergence.py](problem2/problem2_alns_iteration_convergence.py) | 收敛实验：1000 次、种子 20260924、均衡权重 `(0.25, 0.25, 0.25, 0.25)` | `problem2_alns_iteration_convergence.xlsx` 和 `problem2_alns_iteration_trace.csv`；120/500/1000 是同一随机轨迹的检查点，不是三次独立实验 |
| [problem2_alns_5seed_stability.py](problem2/problem2_alns_5seed_stability.py) | 稳定性：20260924—20260928 五个种子，各 1000 次，沿用均衡权重 | `problem2_alns_5seed_stability.xlsx`、汇总/轨迹 CSV 及 `alns_5seed_runs/`；20260924 复用已有收敛记录，另外四个种子独立运行 |
| [problem2_alns_search_diagnostics.py](problem2/problem2_alns_search_diagnostics.py) | 搜索机制诊断：排序下降时机、模拟退火轨迹、破坏比例及迭代深度 | 保留实现，当前副本未纳入该脚本的独立结果目录；不能据此宣称各配置已复现 |
| [problem2_alns_final.py](problem2/problem2_alns_final.py) | 历史均衡权重包装器：源码实际为 **1500 次**、种子 20260927 | 文件名和旧注释不能代替参数核对；不是正式问题二入口 |
| [problem2_lambda_sensitivity.py](problem2/problem2_lambda_sensitivity.py) | 权重敏感性：一个均衡基线和四个目标优先情景 | 当前继承上述 **1500 次**参数；均衡基线从历史结果读取，未在每次运行中重算 |
| [problem2_nsga2.py](problem2/problem2_nsga2.py) | NSGA-II 多目标备选算法 | `problem2_nsga2_result.xlsx`；延误指标与正式 ALNS 的统计范围不同，不能直接排名 |
| [problem2_cpsat_resource_refinement.py](problem2/problem2_cpsat_resource_refinement.py) | 固定架次结构的 OR-Tools CP-SAT 资源调度概念验证 | `problem2_cpsat_resource_refinement_result.xlsx`；不是正式 ALNS 内层，也不是问题二主要求解方法 |

上表中的问题二工作簿/CSV 均在 `problem2/results/`。以下比较边界应随实验表一起保留：

- **1000 与 1500 次不能混用。** 历史权重表同时保留 `problem2_lambda_sensitivity 20260924 1704-1000.xlsx` 和 `problem2_lambda_sensitivity 1500.xlsx`。两表的 Z1 优先情景分别为 `Cmax=6955.47652001904 s` 与 `7021.476520019036 s`；正式结果采用前一组对应的 1000 次正式入口。历史文件名不保证当前脚本仍能重建对应版本。
- **均衡权重的五种子实验不能当作正式延误优先配置的五次验证。** `problem2_alns_5seed_stability.xlsx/Summary` 给出了五条历史记录；首个种子的 `Final_Solution_Detail_Saved=False`，复用汇总和轨迹不代表该表保存了全部五个最终解明细。
- **NSGA-II 的 Z1 范围较窄。** `evaluate_objectives()` 只累加没有硬截止约束的货箱；正式 ALNS 对全部货箱按紧急优先系数加权期望时刻延误。各自得到的 Z1 和综合表现不能在未统一口径时直接解释为优劣。
- **CP-SAT 固定运输结构。** 读取 `problem2_alns_repair_optimized_result.xlsx`，固定货箱集合、访问顺序和机型，仅调整实体无人机、同型共享电池和开始时刻。时间离散为 0.01 s，时长/偏移向上取整、截止时间向下取整；默认时限 60 s、8 workers。它没有联合搜索新的运输路径，不能包装为正式流程的全局最优解。
- **不同权重下的 F 不直接可比。** 权重表列名为 `F_under_own_weights`；应并列查看原始目标向量、可行性与计算预算。

## 问题三：后评价与未完成的权重重求解

| 脚本 | 实际作用 | 归档状态 |
|---|---|---|
| [problem3_posthoc_weight_tradeoff.py](problem3/problem3_posthoc_weight_tradeoff.py) | 读取已完成且存有最终检查记录的目标向量，去重、Pareto 筛选，再按四组权重评分；不调用 ALNS/VNS 或通信重算 | `problem3/results/problem3_posthoc_weight_tradeoff.xlsx` 已归档 |
| [problem3_weight_experiment.py](problem3/problem3_weight_experiment.py) | 各权重分别搜索；当前配置为 ALNS 100 次、VNS 5 轮、种子 20260925 | 整组实验未完成，且当前缺少历史 case_1 独立通信缓存及 manifest，不能宣称可完整重跑 |

后评价归档 `Counts` 表记录：扫描 1185 条含目标信息的记录，5 条最终检查记录符合纳入条件，去重后为 4 个目标向量，其中 2 个非支配向量。ALNS/VNS 的 LEVEL1 历史记录、中断日志和几何缓存不作为独立精审解纳入；只有对应的最终检查记录可以进入候选池。这里的“精审”指代码中的 LEVEL2 数值通信检查，不是任意连续时刻通信可用性的解析证明。

四组权重在这个有限候选池内均选择 `P3-VNS-20-FINAL`，只支持“现有候选的再评分结果”，不支持“改变权重重新优化仍得到同一最优解”。`problem3/results/weight_tradeoff_100iter/case_1/case_1_alns.xlsx` 保存了一次完成的 100 轮 ALNS 结果，不能据此宣称四组 ALNS+VNS 全部完成。另有历史长实验在 458 次迭代中断，相关日志仅保留在本地待审证据区。

正式 ALNS 源码还保留一个已知口径问题：`FORMAL_WARM_F` 用四项等权 `0.25` 计算，默认搜索权重是 `(0.55, 0.15, 0.15, 0.15)`。该常量参与初始温度与最终基准比较，可能影响搜索及“改善”判断；本轮没有修改。展示时应报告同口径的原始目标向量，不能把两个权重体系的 F 之差当作算法性能提升。

## 运行与复核

所有脚本都可由根目录 `run.py` 按脚本名定位；输入、缓存信任和隔离输出规则见 [运行说明](../docs/running.md)。CP-SAT 另需 `requirements-experiments.txt` 中的 OR-Tools。大多数实验会启动搜索，不能把 `run.py` 下存在入口视作轻量复核命令。

以下仅列出后评价命令，其不会搜索或重建通信几何；需要本地待审归档齐全。执行状态以 [Phase 4 报告](../docs/phase4_report.md) 为准：

```powershell
.\.venv\Scripts\python.exe -B run.py problem3_posthoc_weight_tradeoff
```

未重新运行的扩展实验保留历史结果和方法说明，不提供新的耗时、性能提升、显著性或跨机器复现结论。后续若开展比较，应先固定目标口径、随机种子、迭代/时间预算和输入哈希，再将新结果写入独立 runs 目录。
