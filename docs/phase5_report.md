# Phase 5 可复现性验收与公开安全报告

验收日期：2026-10-16。范围：当前发布候选目录；原始竞赛材料仅只读访问。本轮没有优化算法重设计、完整ALNS/VNS、正式结果重建、Git提交/上传或LICENSE添加。

## 1. 最终评估与适用范围

**READY：仅限拟清单中的自有源码、原创文档和实验数值摘要，可作为附有复现限制的代码展示仓库公开。** [A/B/C验收清单](phase5_release_checklist.md)的必须项均通过。不是“完整数据包已获授权”或“四问题端到端已重现”的结论。

用户已明确确认“这个代码全部我写的”，见[作者确认记录](phase5_authorization.json)。因此源码共同作者授权不再列为阻塞，不要求额外队员授权。当前也没有发现禁止展示自有实验统计摘要的具体第三方条款；不因泛化的历史待审标签推定源码或原创数值摘要不能公开。未选择LICENSE，意味着本仓库未主动授予通用开源复用许可，不等于自有源码不能展示。

**完整本地目录整包公开仍为BLOCKED。** 原始Excel、DEM、论文、竞赛题目/模板、工作簿、缓存、原图片等材料的再分发和隐私审核尚未完成；它们不在本次READY范围。不能压缩整个目录代替按清单发布。完整优化复现状态为“尚未重新验证”，不是失败或通过。

## 2. 验收依据与变更范围

本轮重新读取README、Phase 3/4报告、running、provenance、formal_versions、review_required、dependencies和实际源码/测试/工作簿；没有将历史报告中的成功项直接继承为本轮通过。

- 修改README、running和publication_review，反映新环境证据、源码作者确认、公开数据范围和命令状态。
- 修正methodology技术路线中Q2运输轨迹到通信计算的依赖箭头；修正commands.csv的CP-SAT实际输入名为 `problem2_alns_repair_optimized_result.xlsx`。均为文档修正。
- 新增[6项保护测试](../tests/test_phase5_safety.py)、[只读归档核验工具](../scripts/verify_archives.py)、本报告、验收清单、[完整复现计划](phase5_full_reproduction.md)、作者记录和机器可读验收摘要。
- 每个文件的前后SHA见[变更表](phase5_changes.csv)；拟公开路径、大小和哈希见[候选文件清单](phase5_candidate_files.csv)。自引用清单的哈希使用显式标记，本地最终审计另保存完整哈希，避免伪造自哈希。

原有44份Python文件、requirements、路径管理和运行器均未改变；核心模型、约束、目标、算子、随机种子及正式参数未变。`.gitignore`本轮无需修复：实际待审材料全部被现有规则排除。新测试不替换预期答案，不生成实验假数据。

## 3. 独立环境验证

新环境位于 `.release-audit/phase5/env/`，由Python 3.9.10创建，`include-system-site-packages=false`、用户site关闭，全部核验依赖位于该环境目录。已有开发环境只用于调度审计和只读表格比较；新环境承担测试、模块导入、DEM读取、Q4报告运行和归档核验。没有安装/卸载全局包，也没有修改系统Python。

首次主依赖安装失败：旧pip 21.2.4与本机代理组合触发 `ValueError: check_hostname requires server_hostname`。保留失败日志；重试仅为安装子进程设置已有本地HTTP代理的CONNECT入口，未改系统代理、未关闭TLS证书验证。主流程和OR-Tools可选依赖随后安装成功，两次 `pip check` 均返回 `No broken requirements found.`。新环境验证确实完成，不能把失败的第一次尝试隐去，也不能把旧开发环境运行当作干净安装证据。

| 依赖 | 本轮新环境版本 | 作用 |
|---|---|---|
| numpy / pandas | 2.0.2 / 2.3.3 | 数值、表格 |
| matplotlib / Pillow | 3.9.4 / 11.3.0 | 绘图、图像 |
| openpyxl | 3.1.5 | 工作簿读取与写入 |
| rasterio / pyproj / affine | 1.4.3 / 3.6.1 / 3.0.1 | DEM、测地和栅格变换 |
| gurobipy | 12.0.3 | Q1集合划分 |
| networkx | 3.2.1 | Q4任务关联 |
| ortools | 9.15.6755 | 可选CP-SAT扩展 |

实际源码没有SciPy导入，不为目录完整而增加依赖。上述安装元数据支持Python 3.9；本轮只实际验证3.9.10/Windows，不外推到所有Python版本或平台。Matplotlib/PyParsing弃用警告不影响本轮退出状态，保留在本地日志。

Gurobi在新环境中成功建立真实选定批次对应的18变量、80约束模型，未跳过、未调用 `optimize()`。这证明本机当时的模型构建/许可初始化可用；没有核验19525候选的完整优化许可、许可期限或其他机器许可。许可文件、身份信息及原始日志不进入公开清单。

## 4. 实际执行的命令与结果

以下 `$uavPy` 指本轮全新环境的解释器。原始路径在公开文档中以 `.\authorized-uav-data` 代称；执行时使用了本机已有正式输入的真实路径。原始材料只读使用，不因本地核验而推定再分发权限。日志均保留在本地被忽略的审计/runs目录，公开摘要见[机器可读验收证据](phase5_verification.json)。

```powershell
# 本轮曾以已有3.9.10解释器创建独立环境；不要重复覆盖现有env
.\.venv\Scripts\python.exe -B -m venv .release-audit/phase5/env
$uavPy = (Resolve-Path '.release-audit/phase5/env/Scripts/python.exe').Path
& $uavPy -B -m pip install -r requirements.txt
& $uavPy -B -m pip install -r requirements-experiments.txt
& $uavPy -B -m pip check
$env:UAV_RAW_DIR = '.\authorized-uav-data'
& $uavPy -B -m unittest discover -s tests -p test_release.py -v
& $uavPy -B -m unittest discover -s tests -p test_phase5_safety.py -v
```

| 项目 | 实际结果 | 本地证据 |
|---|---|---|
| 新venv创建 | exit 0，25.737 s | `.release-audit/phase5/venv_creation.json` |
| 主依赖首次安装 | exit 2，代理兼容错误；未将其计为通过 | `install_main.json/log` |
| 主依赖重试 / 可选安装 | exit 0；307.400 / 33.636 s | `install_main_retry` / `install_optional_retry` 的json/log |
| 两次依赖一致性检查 | exit 0；无损坏依赖 | `environment_main.json` / `environment_optional.json` |
| 原15项测试 | 15/15通过，无跳过；unittest计时110.323 s | `results/runs/phase5_tests_baseline/unittest.log` |
| 新6项保护测试 | 6/6通过，无跳过；38.742 s | `results/runs/phase5_tests_protection/unittest.log` |
| 仅含文本的公开副本 | 41入口、41模块导入、6项保护测试通过；无raw/reference/cache | `.release-audit/phase5/source_only.json`；保护测试48.533 s |
| DEM只读入口 | exit 0，3.345 s；1309×1486，EPSG:4326；1输入、0产物 | `results/runs/phase5_read_dem/run_manifest.json` |
| Q4非优化报告入口 | exit 0，26.867 s；9输入、11产物，25项检查通过 | `results/runs/phase5_problem4_final_paper_results/run_manifest.json` |
| 正式归档核验 | exit 0；41项全PASS、0失败，37.867 s；资源入口含加载3.116 s，1008项一致 | `results/runs/phase5_archive_audit/summary.json` |

实际小规模/非优化命令如下。为验证工作目录独立性，DEM和Q4报告执行时CWD位于本地审计目录，运行器路径解析为绝对路径；这里给出项目根下的等价命令。输出目录必须不存在，不应复用本轮已经生成的目录。

```powershell
& $uavPy -B run.py --list
& $uavPy -B run.py read_dem --raw-dir '.\authorized-uav-data' --run-dir results/runs/phase5_read_dem
& $uavPy -B run.py problem4_final_paper_results --run-dir results/runs/phase5_problem4_final_paper_results
& $uavPy -B scripts/verify_archives.py --raw-dir '.\authorized-uav-data' --output-dir results/runs/phase5_archive_audit
```

新增6测试覆盖已有run拒绝复用、缺失raw的明确错误和失败manifest、路径模块/运行器导入无输出副作用、mock reference的unlink/rename/replace保护、raw只读保护、未知pickle在 `pickle.load` 前被拒绝。破坏性保护检查使用临时fixture，未尝试删除真正的reference。41模块导入在无数据副本中也通过，不是只检查语法。

无数据副本测试时包含89份文本文件；其后新增的归档工具另在新环境完成实际执行，最终文档与清单再独立检查。

原15测试读取正式结果和显式外部数据；公开克隆缺少冻结工作簿，不能完成该组归档检查。仅缺raw时其中两项会跳过，跳过不等于通过。公开克隆可直接执行6项无正式数据保护检查。

## 5. 分层复现结论

| 层级 | 结论 | 不包含的内容 |
|---|---|---|
| Level 1 静态 | 46份Python的3.9语法、导入根、入口、冻结参数、路径、依赖与文档核对通过 | 语法通过不证明运行结果 |
| Level 2 轻量测试 | 新环境15+6测试、41模块导入通过；无数据副本6测试再次通过 | 不是大规模算法搜索 |
| Level 3 正式归档 | 67指标、货箱覆盖、目标聚合、资源区间、通信表对应、分区/Pareto归档复核通过 | 归档PASS标记不等于重新运行所有业务可行性函数 |
| Level 4 小规模实际运行 | DEM读取、Gurobi真实小模型构建、Q4固定时序资源核算和非优化报告运行通过 | 未通过缩短正式迭代数伪称算法复现；未重算无线几何 |
| Level 5 完整优化 | **尚未重新验证** | Q1完整MILP、Q2正式ALNS、Q3 ALNS/VNS、全量缓存、四问题端到端与完整Q4枚举未运行 |

[完整复现计划](phase5_full_reproduction.md)逐问题列出输入、许可、真实命令、历史耗时、预计产物和比较办法。历史耗时不是本机预测。固定基准断言、候选依赖与新旧输入回退仍有局限，不删除断言、不修改正式参数来“跑通”。

## 6. 正式结果与统计口径

| 问题 | 本轮复核 | 关键归档数值 |
|---|---|---|
| Q1 | 批次与分配表80箱唯一覆盖、18批次、行分量与汇总一致 | 59.1286907835445 kWh；累计作业32776.0157489162 s |
| Q2 | 20架次80箱、送达字段与Q1元数据对应；逐箱延误和硬时限；UAV/电池占用不冲突；16归档PASS | 最大完工6955.47652001904 s；65.6355317188857 kWh；加权延误0 |
| Q3 | 干净20运输/3中继架次；联合目标、能源分量与实体资源；13归档PASS；综合表215条通信区间，其中104条中继区间，与飞行/服务窗口对应 | 资源8/14/2/3；联合最大完工6985.31006777659 s；68.7710956385158 kWh；归档未覆盖0 s |
| Q4 | 由航线恢复7区块；独立枚举核对K2=63、K3=301；14维支配重算Pareto=27/58；现有资源入口重核126子集×8资源 | K2-032资源9/15/3/3，69.5308043960809 kWh；K3-007资源10/16/4/4，71.5252433612119 kWh |

Q1为批次时长总和，不能与Q2/Q3并行最大完工时间直接作提速比较。Q3区间相邻关系的数值核对没有重新评价DEM遮挡/链路预算；归档采用1 s采样和已检测边界0.01 s细化，不能表述为任意连续时刻解析证明。Q4沿用Q3固定时序，未证明分区缩短完工时间。表中资源顺序为运输UAV、电池、中继UAV、能源组件。

Q4新报告与正式表对比：11张表、619单元格，无超出 `1e-9` 绝对容差的差异；50个数值单元格仅有浮点尾差，最大 `4.3655745685100555e-11`；10份LaTeX表逐字节相同。新xlsx字节不同，不称工作簿哈希一致。正式原件从未被替换。

## 7. 公开安全、依赖和Git范围

当前候选及父目录没有实际Git仓库。复用本地被忽略的独立Git元数据，通过只读 `ls-files --others --exclude-standard` 和 `check-ignore` 解析规则；没有在项目创建Git仓库、修改远程配置或执行推送。

[拟跟踪清单](phase5_candidate_files.csv)逐文件给出路径、字节数、SHA和范围。该清单不等于已经stage；未来任何新增文件及真实Git索引需再次核查。本轮没有绕过忽略规则。安全检查覆盖凭据、个人路径/联系方式、文件类型、大文件/重复、符号链接、已知Office个人元数据、pickle入口和待审清单。

- 未发现拟清单中可确认的API Key/Token/密码/许可证凭据或个人Windows路径；文档中的绝对路径为授权输入占位示例。
- 70份本地待审材料哈希保持一致且全部被忽略，另124份历史登记材料未复制；15张图仍未复制，无README图片链接。
- 拟公开文件全部为文本，无Office/DEM/地图/原图/pickle/日志/压缩包/依赖环境。49份本地工作簿的元数据中存在11份个人/未分类作者值与11份保存路径命中，均在隔离范围；未在报告输出具体个人值。
- 现有缓存有冻结来源指纹及输入校验；反序列化前验证来源，未知pickle测试拒绝。135/210航段覆盖不是全量缓存；扩展独立权重缓存缺失未补造。用户提供某个SHA只能表达信任声明，不证明未知文件安全。
- 原始输入不可随公开仓库取得；外部raw及授权材料按清单提供。综合提交表映射有效，缺失历史模板没有伪造。缺少外部材料会阻止相应复核/求解，不影响明确限定的源码展示。

关键词扫描不是法律结论，也不保证未识别秘密绝对不存在。当前没有具体未授权材料进入拟公开清单；因此不把隔离材料的未定权限转化为源码展示的阻塞。若未来解除忽略或加入图片/工作簿，须重新审核。

## 8. 文档一致性与渲染

README路径、相对链接、入口名、PowerShell语法及正式指标与实现核对。Mermaid保持Q3 ALNS/VNS独立分支；methodology补全Q2轨迹到通信几何依赖。CP-SAT输入名修正仅改变文档。

本地Mermaid/KaTeX语法检查与Chrome headless截图目视核对分开记录：截图已显示3个Mermaid图、7个块公式和55个行内公式；同次 `--dump-dom` 在异步完成前抓到pending，不能宣称自动完成标记验证成功。GitHub服务器实际渲染尚未验证。最终链接/命令计数见验收JSON及本地document审计文件；没有外部图片或失效的待审图片链接。

## 9. 完整性终检与限制

原始3个材料目录共1745个文件：文件集合、SHA-256、大小、mtime和mode与既有快照一致，无新增、删除或变化。36份正式reference的哈希和属性保持；70份冻结/隔离材料与来源哈希一致。原44份Python文件哈希不变，新增工具只负责验收，不进入原41入口清单。完整性证据为 `.release-audit/phase5/integrity.json` 和公开验收摘要。

本轮尚未执行：长时间优化、通信几何全量重建、跨平台/多Python版本、GitHub线上渲染及远程发布。已知数学/搜索口径问题继续保留在[known_issues](known_issues.md)与[扩展实验说明](../experiments/README.md)，没有擅自修复。

下一步建议先保持当前文本公开范围；若决定授权完整材料或执行Level 5，再分别进行数据审批和带预算的优化复现。LICENSE由作者选择，算法问题单独评审。Phase 5到此停止，不自动进入发布。
