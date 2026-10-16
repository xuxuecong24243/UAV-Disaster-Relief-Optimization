# 运行与复现说明

当前目录是按白名单构建的公开副本，只含源码、原创文档与数值摘要，不含原始 Excel、DEM、正式工作簿、图片或缓存。当前范围和本次实际验证见 [Phase 6 报告](phase6_report.md) 与 [发布白名单](phase6_release_manifest.csv)。Phase 2—5 的报告、清单和哈希是原候选项目的历史记录，不代表公开副本具有相同的本地材料。

此文档区分无私有数据的基本检查、授权材料下的归档复核和重新求解。四个问题尚未完成端到端复现，长实验不能当作轻量检查执行。历史证据见 [Phase 3](phase3_report.md)、[Phase 4](phase4_report.md)、[Phase 5](phase5_report.md) 报告，命令接口清单见 [commands.csv](commands.csv)。Phase 2 清单中的源码哈希代表整理前版本，工程变更另存 [phase3_changes.csv](phase3_changes.csv)。

## 环境

历史及本次验收目标环境为 Windows / Python 3.9.10；本次实际结果以 Phase 6 报告为准。`requirements.txt` 固定主流程及绘图直接依赖；`requirements-experiments.txt` 额外包含 CP-SAT 扩展所用 OR-Tools。没有 SciPy 导入，因此未添加。直接使用的 Pillow 已补充声明。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# 需要 CP-SAT 扩展时再安装：
.\.venv\Scripts\python.exe -m pip install -r requirements-experiments.txt
```

以上命令仅用于新克隆或不存在 `.venv` 的目录，不要覆盖已有环境。Phase 3/4 曾复用系统包；**Phase 5 已在独立新环境、`include-system-site-packages=false` 下实际安装主流程和可选依赖，且两次 `pip check` 均通过**。首次旧 pip 的代理连接失败后，仅调整安装进程的代理方式完成重试，未改系统设置。所有直接依赖的元数据允许 Python 3.9；新版本 Python 和其他操作系统尚未验证。实际记录见 [Phase 5 报告](phase5_report.md)。

Gurobi Python 包与可用许可证是两个条件。问题一集合划分需要 Gurobi；请按所在机构或个人合法许可配置本机许可证。不要提交 `gurobi.lic`、WLS 密钥或令牌。历史 Phase 5 轻量测试通过真实问题一建模函数构建 18 个正式已选候选、80 条覆盖约束的模型，未调用 optimize，不能据此声称完整求解许可或最优结果已验证。问题二、三的正式搜索使用 Python 启发式算法，问题四使用枚举和区间资源核算。

## 公开副本的最小运行

以下命令不需要正式数据、DEM 或通信缓存。合成示例只调用现有区间资源分配函数，不进行优化搜索。

```powershell
.\.venv\Scripts\python.exe -B run.py --list
.\.venv\Scripts\python.exe -B scripts/demo_public_intervals.py
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

入口列举预期为 41 个。示例使用原创区间 `A:[0,4]`、`B:[1,3]`、`C:[3,5]`、`D:[5,7]`，标记 `example=synthetic_public_demo`、`competition_result=false`，预期最少资源数 2；它不代表正式竞赛运输或通信结果。Phase 6实测24项中15项通过、9项归档依赖检查跳过，0失败/错误，unittest耗时44.737 s。记录见Phase 6报告；跳过不计为通过。

## 输入与输出

默认根目录由 `project_paths.py` 自身位置确定。输入读取不依赖启动 CWD，也不会自动搜索原竞赛目录。

| 类型 | 默认位置 | 覆盖方式 |
|---|---|---|
| 原始五份 Excel | `data/raw/` 平铺，或其 `无人机应急物资运输基础数据/` 子目录 | `--raw-dir` / `UAV_RAW_DIR` |
| DEM | `data/raw/dem/镇龙乡及周边30米DEM.tif` | 同一 raw 根，也支持原始地理数据子层级 |
| 中间输入 | `data/processed/problemN/` | `--input-dir` 指向含 `problemN` 子目录的根 |
| 正式结果作为输入 | `results/reference/problemN/` | 中间输入未提供时读取冻结版本 |
| 扩展实验归档 | `experiments/problemN/results/` | 仅当前三类未命中时读取 |
| 新结果 | `results/runs/<UTC时间_随机标识>/problemN/` | `--run-dir` 只接受 runs 内尚不存在的目录 |

输入优先级为：显式 input 根 → processed → reference → experiments 归档。只有所有候选位置都缺失时才报错。`--input-dir` 只能指定一个根，且不是“禁止回退”开关；该根缺少某文件时仍会读取后面的冻结来源。多步重算应汇集已确认的新产物，不能只传最后一步的输出目录。具体做法见[从原始输入构建中间依赖](#从原始输入构建中间依赖)。

原始数据和 DEM 尚未获公开授权，未复制进仓库。以下示例中的 `.\authorized-uav-data` 是相对于启动目录的占位路径，应替换为持有使用权限的原始 `data` 根。`results/reference/`、`review_hold/` 及实验归档只说明本地可选映射，当前公开副本不包含其中任何数据文件。读取外部输入不会改动外部文件。

统一入口接受保留的 41 个脚本名。可从其他工作目录通过 `python <项目绝对路径>/run.py ...` 启动；命令行中显式给出的相对目录仍相对于调用者 CWD，内部默认路径相对于项目根。

每次运行写出 `run_manifest.json`，记录命令、源代码哈希、包版本、实际读取文件的路径/哈希、输出哈希和成功/失败状态。清单属于本地运行资料，可能含个人路径，默认被 Git 忽略。直接运行旧脚本也使用统一路径和保护，但建议用 `run.py` 获得完整来源清单。

`reference`、raw/processed、隔离缓存及实验归档有 Python IO 写保护；输出 helper 拒绝当前独立运行目录以外的路径。问题四需要追加旧论文工作簿时先复制至 runs。`run.py` 拒绝重复使用已存在的运行目录。

## 授权材料下的归档复核与可视化（公开副本不能独立完成）

以下命令需先取得并按映射配置授权的冻结结果、原始输入和缓存，再在项目根目录执行。所列成功记录属于历史阶段，本次未在无数据副本重新运行这些归档检查。`run.py --list` 是唯一不需要归档材料的命令。

```powershell
# 已验证：列出 41 个入口，不启动实验。
.\.venv\Scripts\python.exe -B run.py --list
# Phase 3 已实际验证：利用本地冻结表重算 Q4 最终报告，不启动全枚举。
.\.venv\Scripts\python.exe -B run.py problem4_final_paper_results
# Phase 3 已实际验证：提供外部原始数据，只读检查 DEM。
.\.venv\Scripts\python.exe -B run.py read_dem --raw-dir '.\authorized-uav-data'
```

Q4 报告在 Phase 3 的结果为 25 项检查通过，619 个单元格按 `1e-9` 绝对容差与归档一致，10 份 LaTeX 逐字节一致。它仍依赖已有 Q4 分区、筛选及辅助表；运行报告模块不等于重做任务枚举。Phase 4 额外验证了从项目外 CWD 调用 `run.py --list`，不代表全部 41 个入口都已从外部 CWD 端到端运行。

```powershell
# 静态检查通过、尚未执行：读取干净 Q3 VNS 表及相应地理输入绘图。
.\.venv\Scripts\python.exe -B run.py plot_problem3_final_results --raw-dir '.\authorized-uav-data'
```

Q2 正式入口会执行 1000 次搜索后绘图，不能把 `problem2_final_run_and_visualization` 当作“仅绘制已有结果”的快速命令。所有新图、表及日志写入独立 runs，公开图像仍需另行审查来源、地理数据权限和个人信息。

## 四个问题的运行关系

- 问题一：`problem1_segment_preprocessing` → `problem1_max_safe_payload` → `problem1_batch_optimization`。正式结果仍可作为后续步骤的只读基线。
- 问题二：`problem2_final_run_and_visualization` 是正式入口；1000 次、种子 20260927、权重 `(0.55,0.15,0.15,0.15)` 保持不变。它读取问题一正式批次及航段。扩展入口 `problem2_alns_final` 仍为原有 1500 次，不能当作正式入口。
- 问题三：通信检查/覆盖/基线/精英池、`problem3_alns` 和 `problem3_vns` 各自保留；VNS 从 Q2 运输解恢复，不从 ALNS 最终结果启动。ALNS 默认 `--phase b` 实际为 500 次；其帮助文字中的“100-iteration”过时，见[已知问题](known_issues.md)。VNS 为 20 个循环。最终绘图和填表读取仅有 20 条有效运输架次的干净 VNS 工作簿。
- 问题四：`problem4_service_zone_graph`、`problem4_group_scheduler`、`problem4_full_enumeration` 及报告模块分别定位。通信区间明确读取 `results/reference/submission/结果提交.xlsx` 的 `Q3_通信保障`，没有伪造缺失的历史同名模板。

`fill_q3_submission` 还需 `--template <获授权的结果提交模板.xlsx>`；仅填 Q3 页，不代表能够生成综合提交表全部内容。缺失模板会在耗时通信复核前报错。

## 从原始输入构建中间依赖

以下是 **B 模式：静态检查过接口与文件名、全链尚未执行**。会启动正式优化及高成本通信计算，应在另行评估计算预算后逐步运行。本阶段没有执行下面的求解命令。已有参数保持不变；不能因重新求解未得到归档答案而修改模型或预期值。

在项目根目录的 PowerShell 中准备一个独立工作批次，`inputs/problemN` 仅汇集本批次产生的输入副本。辅助复制函数拒绝覆盖目标，校验源/目标 SHA-256，并登记来源；这些资料都留在被忽略的 runs 内。

```powershell
$uavPython = (Resolve-Path '.\.venv\Scripts\python.exe').Path
$uavRaw = '.\authorized-uav-data'
$uavChain = Join-Path (Get-Location).Path ('results/runs/rebuild_' + [guid]::NewGuid().ToString('N'))
$uavInputs = Join-Path $uavChain 'inputs'
New-Item -ItemType Directory -Path "$uavInputs/problem1", "$uavInputs/problem2", "$uavInputs/problem3" | Out-Null

function Copy-UavStageInput {
    param([string]$Stage, [string]$Problem, [string]$Filename)
    $source = Join-Path $uavChain "$Stage/$Problem/$Filename"
    $target = Join-Path $uavInputs "$Problem/$Filename"
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw "缺少新产物：$source" }
    if (Test-Path -LiteralPath $target) { throw "拒绝覆盖已有输入副本：$target" }
    Copy-Item -LiteralPath $source -Destination $target
    $sourceHash = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash
    $targetHash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
    if ($sourceHash -ne $targetHash) { throw '输入副本哈希不一致' }
    [pscustomobject]@{Source=$source; Target=$target; SHA256=$sourceHash} |
        Export-Csv -LiteralPath "$uavChain/input_copies.csv" -NoTypeInformation -Append -Encoding UTF8
}
```

先生成并逐项汇集 Q1 三份产物，再运行 Q2。每条命令成功后才能执行紧随的复制；任一步失败，应停止并检查该运行的 `run_manifest.json`。

```powershell
& $uavPython -B run.py problem1_segment_preprocessing --raw-dir $uavRaw --run-dir "$uavChain/q1_segments"
if ($LASTEXITCODE -ne 0) { throw 'Q1 航段步骤失败' }
Copy-UavStageInput q1_segments problem1 segment_preprocessing.xlsx

& $uavPython -B run.py problem1_max_safe_payload --raw-dir $uavRaw --input-dir $uavInputs --run-dir "$uavChain/q1_payload"
if ($LASTEXITCODE -ne 0) { throw 'Q1 载荷步骤失败' }
Copy-UavStageInput q1_payload problem1 problem1_max_safe_payload.xlsx

& $uavPython -B run.py problem1_batch_optimization --raw-dir $uavRaw --input-dir $uavInputs --run-dir "$uavChain/q1_batches"
if ($LASTEXITCODE -ne 0) { throw 'Q1 集合划分步骤失败' }
Copy-UavStageInput q1_batches problem1 problem1_batch_optimization.xlsx

& $uavPython -B run.py problem2_final_run_and_visualization --raw-dir $uavRaw --input-dir $uavInputs --run-dir "$uavChain/q2_formal"
if ($LASTEXITCODE -ne 0) { throw 'Q2 正式 ALNS 失败' }
Copy-UavStageInput q2_formal problem2 problem2_Z1_priority_result.xlsx
```

此时 `inputs/problem1` 同时包含航段、载荷和组批，`inputs/problem2` 包含本次 Q2 结果。只传 `q1_payload` 给后续流程会丢失前一步航段的覆盖路径，并可能退回冻结航段，这正是要避免的混用。请核对每次 manifest 的 `inputs` 路径和哈希；存在回退到 `results/reference` 的文件时，不应宣称该步骤完全来自新输入。

Q3 的新输入需按“通信中断 → 中继覆盖 → 基线 → 精英池 → 新通信缓存”构建。下面继续使用同一 `$uavInputs`，不是改用每个阶段独立的输出根。

```powershell
& $uavPython -B run.py problem3_core --raw-dir $uavRaw --input-dir $uavInputs --run-dir "$uavChain/q3_communication"
if ($LASTEXITCODE -ne 0) { throw 'Q3 通信中断检查失败' }
Copy-UavStageInput q3_communication problem3 problem3_transport_communication_check.xlsx

& $uavPython -B run.py problem3_relay --raw-dir $uavRaw --input-dir $uavInputs --run-dir "$uavChain/q3_coverage"
if ($LASTEXITCODE -ne 0) { throw 'Q3 中继覆盖步骤失败' }
Copy-UavStageInput q3_coverage problem3 problem3_relay_coverage.xlsx

& $uavPython -B run.py problem3_baseline --raw-dir $uavRaw --input-dir $uavInputs --run-dir "$uavChain/q3_baseline"
if ($LASTEXITCODE -ne 0) { throw 'Q3 基线步骤失败' }
Copy-UavStageInput q3_baseline problem3 problem3_baseline.xlsx

& $uavPython -B run.py problem3_alns --raw-dir $uavRaw --input-dir $uavInputs --run-dir "$uavChain/q3_elite" -- --phase a
if ($LASTEXITCODE -ne 0) { throw 'Q3 精英池步骤失败' }
Copy-UavStageInput q3_elite problem3 problem3_relay_elite_pool.xlsx

& $uavPython -B run.py problem3_segment_communication_cache --raw-dir $uavRaw --input-dir $uavInputs --run-dir "$uavChain/q3_cache" -- --full --workers 1
if ($LASTEXITCODE -ne 0) { throw 'Q3 新缓存构建失败' }
$uavCache = Join-Path $uavChain 'q3_cache/problem3/problem3_communication_geometry_cache.pkl.gz'
$uavCacheHash = (Get-FileHash -LiteralPath $uavCache -Algorithm SHA256).Hash
```

这些步骤仍带有竞赛算例约束：精英池保留固定基线候选，Phase A 核对基线目标，ALNS/VNS 核对固定暖启动目标和候选。新 Q2 解即使可行，也未必满足这些断言。失败后保留产物和错误，列入后续分析；不能取消断言或用旧数据补成“全链成功”。

对于**刚刚由本地受信代码生成**的新缓存，下一进程显式传入路径与其 SHA。VNS 与 ALNS 是两个独立分支，均读取相同 Q2 运输基因；选择需要评估的一支运行，不应默认一起启动。

```powershell
# 静态检查通过、尚未执行：正式 VNS 分支，20 个循环。
& $uavPython -B run.py problem3_vns --raw-dir $uavRaw --input-dir $uavInputs --run-dir "$uavChain/q3_vns" --cache-file $uavCache --cache-sha256 $uavCacheHash
# 静态检查通过、尚未执行：另一个独立 ALNS 分支，默认 500 次。
& $uavPython -B run.py problem3_alns --raw-dir $uavRaw --input-dir $uavInputs --run-dir "$uavChain/q3_alns" --cache-file $uavCache --cache-sha256 $uavCacheHash -- --phase b
```

Q4 全链还需把新 Q3 运输解、经复核的通信区间及相应辅助表连接起来。综合提交表的冻结映射只证明归档数据关系，不能自动用于变化后的新时序；在新提交表字段核验前，不把 Q4 归档报告命令当作新解的完整下游复现。该步骤仍需后续实施和验证。

## 通信缓存

冻结来源是 `review_hold/cache/problem3/`，校验基准 SHA-256 保留在 Phase 2 清单。先核对缓存字节和 11 项相关输入完整哈希，再用原模块身份反序列化；未知 pickle 默认拒绝。自行提供其他缓存需显式 `--cache-file` 和 `--cache-sha256`，且调用者必须知道并信任其生成方；哈希本身不证明文件安全。

正式缓存有 135/210 条有向航段及 240 个候选，其余 75 条按原算法按需计算。现有签名函数没有改变；新增完整输入指纹属于读取前的来源检查。运行时先将受信缓存复制进本次 runs，再允许懒更新；不会更新 review_hold 原件。当前进程生成的缓存登记其哈希，跨进程复用新的非冻结缓存需要显式声明信任。

如果重新保存 Excel，即使数值相同，ZIP 元数据也可能使文件 SHA 改变；冻结缓存的 11 项输入指纹会拒绝这些新字节。这是来源保护的预期行为。新输入应建立独立缓存并显式传入可信来源及 SHA，方法见上节；不要修改冻结哈希、原签名算法或把新文件冒充原件。

如只使用冻结中间输入、确需完整重建缓存，可在另行评估计算预算后运行以下**静态检查通过、尚未执行**的命令：

```powershell
.\.venv\Scripts\python.exe -B run.py problem3_segment_communication_cache --raw-dir '.\authorized-uav-data' -- --full --workers 1
```

依赖还包括 Q1 航段、Q2 正式结果和 Q3 精英池。归档 manifest 记录部分缓存累计预处理 `5606.644827` 秒，不能视为当前机器完整重建时间。默认不加 `--full` 会计算 10 条最长航段 benchmark，也不是纯读取检查。Phase 3、4 都未执行完整构建或 benchmark。

`problem3_weight_experiment` 的历史 case_1 独立缓存没有随 Phase 2 复制，当前阻塞；不能用正式缓存默默替代。原始长实验日志还保留中断证据，不能称为完成的权重实验。

## 测试分层与历史记录

```powershell
# 公开副本默认测试：归档依赖项明确 skip，不伪造通过。
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
# 原有六项保护检查也可独立运行：
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_phase5_safety.py -v
```

测试输出只进入 runs，不运行 ALNS/VNS、全分区枚举或通信几何重建。公开测试中归档测试的执行条件已显式检查；缺少工作簿或缓存时跳过，既不修改预期数据，也不把缺失材料导致的跳过计为通过。新增原创区间用例用于核验无数据的固定区间资源分配功能。

只有具备授权材料时，才能尝试原 15 项归档测试：

```powershell
$env:UAV_RAW_DIR = '.\authorized-uav-data'
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_release.py -v
```

`UAV_RAW_DIR` 只配置原始数据，不会补齐缺失的工作簿、通信缓存或中间依赖。Gurobi 环境或许可不可用时建模检查也会明确 skip。对比通过数时必须同时记录跳过项及输入来源。

**历史 Phase 5 记录**：带本地授权材料的新隔离环境中，两组分别为 6/6 通过（38.742 s）、15/15 通过（110.323 s），均无跳过；当时目录发现合计 21 项。这些数值不代表当前公开副本的新测试结果。本次新增用例、公开跳过条件和实际运行结果以 Phase 6 报告为准。

Phase 3 实际记录为 15 项全部通过、47.509 s；Phase 4 在显式配置外部原始数据的同一环境回归，15 项全部通过、87.017 s，0 失败、0 错误、0 跳过。模块导入检查覆盖 41 个模块；测试中的缓存核验与 Q4 资源计数不等于优化重跑。具体日志位置与本轮验收见 [Phase 4 报告](phase4_report.md)。

| 状态 | 范围 |
|---|---|
| 历史已验证 | Phase 3—5 的入口列举、外部 CWD 检查、带授权材料的 15 项回归、DEM 读取与 Q4 报告；当前公开副本未包含相应材料 |
| 静态检查通过、尚未执行 | 本页的完整重算连接方式、Q3 绘图、Q1/Q2/Q3 搜索及通信重建命令 |
| 尚未执行 | 其他 Python/操作系统、全链端到端结果比较；空白环境安装已在Phase5补验 |
| 存在阻塞 | 未随公开候选分发的数据/结果/缓存及授权；Q3 独立权重实验缺失缓存；新解进入 Q3 固定基准断言及 Q4 通信映射尚需核验 |
