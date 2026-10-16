# Level 5 完整优化复现计划

**状态：尚未重新验证。** Phase 5 未执行完整 Q1 求解、Q2 ALNS、Q3 ALNS/VNS、通信缓存重建或四问题端到端优化。以下是经源码核对的执行计划，不能代替实际运行记录。已执行检查见 [Phase 5 报告](phase5_report.md)。

## 前置条件与输入隔离

正式流程依赖见 [requirements.txt](../requirements.txt)，可选 CP-SAT 依赖见 [requirements-experiments.txt](../requirements-experiments.txt)。Phase 5 的新环境实际状态以验收报告为准；其他 Python 版本和操作系统仍需单独验证。Q1 完整集合划分需要适用于 19525 个候选变量的 Gurobi 许可；能导入包或创建小模型不证明完整规模许可可用。Q2/Q3 正式搜索及 Q4 枚举不调用 Gurobi 优化，部分导入链仍需要该包。

运行者须有权在本地使用五份原始 Excel、DEM、相应中间结果与模板；本地使用和公开再分发是不同事项。输入文件名、来源和哈希见 [原始输入清单](../data/raw/expected_inputs.csv)、[dependencies.csv](dependencies.csv)。源码作者已经确认代码全部由本人编写；这不替代第三方数据与材料的权限核对。

不指定 `--input-dir` 时会读取已有 processed/reference/实验归档；这属于冻结输入上的单阶段重跑。完全从原始数据重建时，必须按 [运行说明](running.md#从原始输入构建中间依赖) 逐步生成、复制并校验输入，将新产物汇集到同一批次的 `inputs/problemN/`。`--input-dir` 缺项仍可回退冻结表，需逐项检查 manifest，不能把新旧混用称为端到端成功。

以下命令在项目根运行，使用已经准备好的解释器、合法数据根和中间输入根：

```powershell
$uavPython = (Resolve-Path '.\.venv\Scripts\python.exe').Path
$uavRaw = '.\authorized-uav-data'
# 替换为按running.md实际准备、逐项核验的目录；此示例目录并未自动创建。
$uavInputs = (Resolve-Path 'results/runs/rebuild_batch/inputs').Path
```

这些是后续计划命令，不应一次性粘贴执行。每一步成功后才准备下一步输入；任一步失败就保留日志并停止。默认输出进入新的 `results/runs/`，禁止覆盖 reference。

## Q1：航段、载荷和集合划分

| 步骤 | 必需输入 | 本次run中主要产物 |
|---|---|---|
| 航段 | 节点Excel、DEM | `problem1/segment_preprocessing.xlsx`、`segment_O01_S01_dem_check.png` |
| 最大安全载荷 | 运输无人机Excel、航段表 | `problem1/problem1_max_safe_payload.xlsx` |
| 集合划分 | 需求、运输无人机、航段和最大安全载荷表 | `problem1/problem1_batch_candidates.xlsx`、`problem1_batch_optimization.xlsx` |

```powershell
& $uavPython -B run.py problem1_segment_preprocessing --raw-dir $uavRaw
# 汇集新航段后：
& $uavPython -B run.py problem1_max_safe_payload --raw-dir $uavRaw --input-dir $uavInputs
# 汇集新载荷并确认完整模型许可后：
& $uavPython -B run.py problem1_batch_optimization --raw-dir $uavRaw --input-dir $uavInputs
```

冻结 `optimization_summary` 的历史 `runtime_s=16.6920593` s；三个求解阶段为 0.7826295、1.270408、1.9374135 s。总时间还包括候选构建和导出等工作，不包含所有上游地形预处理，也不是当前机器预算预测。

比较80箱唯一覆盖、19525个候选、18个批次、能耗59.1286907835445 kWh、累计作业时间32776.0157489162 s和各阶段求解状态。保留字典序和源码能耗容差；并列最优批次可能结构不同，不应只用行顺序判断是否复现。

## Q2：正式1000次ALNS

输入为原始需求、机型及节点数据、Q1航段和组批。正式入口锁定1000次、种子20260927、权重 `(0.55,0.15,0.15,0.15)`，不使用1500次扩展包装器。

```powershell
& $uavPython -B run.py problem2_final_run_and_visualization --raw-dir $uavRaw --input-dir $uavInputs
```

预计生成 `problem2/problem2_Z1_priority_result.xlsx`（目标、架次、送达、资源、收敛及可行性表），以及以 `问题2_Z1优先_` 开头的收敛、路线、无人机/电池甘特和送达对比五张图。该入口会先搜索，不能作为纯绘图检查。

冻结 Summary 的 `Runtime_s=431.7919718` s，只代表历史正式运行。比较20架次、80箱唯一覆盖、最大完工6955.47652001904 s、运输能耗65.6355317188857 kWh、加权延误0、资源占用及16项检查。相同种子不保证跨环境轨迹一致，应分别比较目标、解结构和收敛轨迹，保持相同目标尺度。

## Q3：通信依赖与独立搜索分支

输入包括全部原始Excel、DEM、Q1航段、Q2运输解、运输通信检查表、覆盖表、基线表和精英池。新输入依次由 `problem3_core`、`problem3_relay`、`problem3_baseline`、`problem3_alns --phase a` 构建，完整命令及复制检查见运行说明。它们包含几何计算，不能当作无成本的文件准备。

既有可信缓存覆盖135/210条有向航段和240个候选，剩余75条按需计算。冻结manifest的 `Preprocessing_Time_s=5606.6448268` s 是部分缓存累计预处理，不是完整重建时间，也不可不加说明与搜索计时相加。

```powershell
# 如确需为新输入完整重建，另行评估预算后执行：
& $uavPython -B run.py problem3_segment_communication_cache --raw-dir $uavRaw --input-dir $uavInputs -- --full --workers 1
```

预计生成 `problem3_communication_geometry_cache.pkl.gz` 和 `problem3_communication_geometry_cache_manifest.xlsx`。不加 `--full` 会计算默认10条最长航段的benchmark，不是只读检查；本轮两者均未执行。新缓存必须由可信代码实际生成，跨进程使用前按运行说明设置其真实路径 `$uavCache` 与实际SHA `$uavCacheHash`；不能使用未知pickle，哈希本身也不证明任意来源安全。冻结缓存还核对11项输入字节，新Excel即使数值相同也可能触发指纹拒绝，不得改冻结指纹绕过。

```powershell
# 正式VNS：20个循环、种子20260925；两个cache变量必须已按running.md真实设置。
& $uavPython -B run.py problem3_vns --raw-dir $uavRaw --input-dir $uavInputs --cache-file $uavCache --cache-sha256 $uavCacheHash
# 独立ALNS分支：实际默认500次，不是VNS前置步骤。
& $uavPython -B run.py problem3_alns --raw-dir $uavRaw --input-dir $uavInputs --cache-file $uavCache --cache-sha256 $uavCacheHash -- --phase b
```

VNS从Q2恢复运输基因，输出 `problem3_vns_20cycles.xlsx`、同名日志及runs缓存更新；ALNS独立输出 `problem3_alns_final.xlsx` 和日志。历史Summary记录：VNS `Total_Runtime_s=1464.2691627`；ALNS `Iterations=500`、`Total_Runtime_s=5415.1499931`、`Mean_Iteration_Time_s=9.6916452786`。这些均非当前机器预测；旧帮助文本的100次不能替代实际配置。

正式VNS比较20条有效运输架次、80箱、3中继架次，实体资源8/14/2/3（运输UAV/电池/中继UAV/能源组件），联合能耗68.7710956385158 kWh、最大返航完工6985.31006777659 s、延误0。通信还必须比较评价层次、所有可行性、未覆盖时长及检查精度。归档0 s是1 s采样与已检测边界0.01 s细化下的数值结果，不是任意连续时刻的解析证明；LEVEL1占位字段不能替代LEVEL2标准复核。

新Q2可行解未必满足Q3固定候选和暖启动目标断言，不能删除断言来跑通。ALNS固定基准F与默认权重的口径问题保持现状，不能用跨权重F差值包装提升。独立权重扩展另缺自己的缓存及manifest，不得由正式缓存替代。

## Q4：固定时序枚举与报告链

完整枚举需要干净Q3 VNS表、原始需求和综合提交表 `Q3_通信保障`。后者显式读取 `submission/结果提交.xlsx`，没有伪造历史缺失的同名模板。

```powershell
& $uavPython -B run.py problem4_full_enumeration --raw-dir $uavRaw --input-dir $uavInputs
```

预计输出K2/K3的 `all` 与 `pareto` 四份工作簿、`group_subset_results.xlsx`、`relay_duplication_analysis.xlsx`、`problem4_representative.xlsx` 和 `problem4_run.log`。比较7个任务块、126个非空真子集×8类资源、K=2的63个无标签分区、K=3的301个分区、14维支配关系及27/58个非支配方案。固定Q3时序下不比较“分区缩短工期”。

旧论文报告脚本的40.395 s是历史回退常量，不是本机完整枚举计时。候选比较、旧论文表、能耗完工核算、选择分析和最终报告还需各自输入表；按脚本的 `SOURCE_FILES` / `SOURCE_PATHS` 与依赖清单逐项汇集。只运行 `problem4_final_paper_results` 读取旧表不等于重新枚举或完整Q4复现。

如果新Q3时序变化，旧综合表通信区间不能直接沿用。`fill_q3_submission` 只填Q3页且需要合法模板与通信检查，并没有生成整份综合提交表所有页的流水线。新时序到Q4的字段映射须另行核验；未满足前不能宣称端到端成功。

## 比较与审计规则

1. 冻结结果始终按已有SHA核验，不重新生成后覆盖。新Excel的ZIP元数据可能不同，按表格内容比较，不要求新文件字节等同。
2. 整数计数、货箱覆盖、资源和分区集合精确比较；浮点值报告原值、绝对差、相对差和预先声明的容差。现有Q4报告采用绝对容差 `1e-9`，不能未经评估推广到全部几何量和搜索指标。
3. 分别记录输入/配置一致、新解可行、目标一致、结构一致、逐步轨迹一致。相同目标不能代替资源或通信可行性；不同轨迹也不能通过修改答案掩盖。
4. 保存源码/输入/输出哈希、环境版本、真实退出码、运行清单和计时。中断必须记录实际终止原因和已完成迭代，不得把部分实验算作完成。
5. 全部新产物和比较报告进入runs；模型、正式参数与reference保持不变。完整复现与公开材料权限分别验收。
