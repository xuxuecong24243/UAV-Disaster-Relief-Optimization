# Phase 6 独立公开副本验收报告

日期：2026-10-16。最终评估：**READY——可进入人工确认与远程发布**，仅适用于本报告的自有源码、原创文档和允许公开的数值摘要。尚未提交commit、建立远程或上传GitHub，未添加LICENSE。

本轮工作目录为 `UAV-Disaster-Relief-Optimization-public/`。为完整保护来源，Phase 6报告和审计文件全部保存在此独立副本；原候选 `UAV-Disaster-Relief-Optimization/` 及三个竞赛材料目录没有写入。实际机器绝对路径仅保存在本地记录，不写进拟公开文件。

## 1. 按Task 1—8实施及范围

1. **范围核验**：重新读取README、Phase 4/5报告、验收/变更/候选清单、实际源码、依赖和测试。96条Phase 5白名单逐一检查存在、大小、SHA、UTF-8、路径及公开分类，全部符合；其中清单自己的自引用哈希另按实际字节记录。
2. **独立复制**：仅遍历96条白名单逐文件复制，复制后96/96哈希相同。没有先复制整个目录再删除材料。新副本未带入DEM、Excel、论文、图像、缓存、旧环境、日志或原始数据。
3. **公开文档**：README改为公开副本视角，区分无数据示例、真实归档摘要、历史验证和本轮结果；修正目录树及依赖说明。
4. **工程准备**：保留依赖版本、运行器和路径接口；增加合成示例、3项测试、白名单验证器与Git字节保持配置；完善忽略规则。未引入CI/CD或重构模型。
5. **安全初验**：报告文件生成前的100份文本通过检查，41个原模块AST一致；后续106份完整发布集合再次审查。
6. **独立环境实测**：公开副本内新建不继承系统包的venv，安装主流程与可选依赖，实际执行入口、合成示例和24项测试。
7. **本地Git**：初始化main分支，以显式路径清单暂存100份已完成文件，核对索引集合/字节、diff及remote。没有commit或远程操作。
8. **报告与最终门禁**：新增6份验收文档/清单，形成106份最终白名单；再次按白名单暂存、检查字节和安全状态。交付记录见[发布清单](phase6_release_manifest.csv)、[变更表](phase6_changes.csv)、[安全报告](phase6_security_report.md)、[发布检查表](phase6_github_release_checklist.md)、[机器可读证据](phase6_verification.json)。

## 2. 文件组成与改动

最终106份发布文件：49份Python、22份Markdown、21份CSV、8份JSON、2份历史patch、2份依赖txt、`.gitignore`和`.gitattributes`。每个文件的字节数和SHA均列在发布清单。96份有Phase 5来源，10份为本阶段新建；不以忽略规则替代白名单。

新增文件为：

- `scripts/demo_public_intervals.py`：原创四区间示例，直接调用已有Q4资源分配函数。
- `tests/test_public_demo.py`：3项合成区间正确性检查。
- `scripts/verify_public_release.py`：文件集合/类型/哈希及可选Git索引的显式白名单检查。
- `.gitattributes`：禁用自动行尾转换以保留审计字节；历史patch语法中的空白上下文不作为源码空白错误处理。
- `docs/phase6_report.md`、`phase6_release_manifest.csv`、`phase6_security_report.md`、`phase6_changes.csv`、`phase6_github_release_checklist.md`、`phase6_verification.json`。

修改集中于README/running、忽略规则、历史文档的示例路径和测试执行条件。39处原示例盘符路径在公开副本中改为相对占位；原候选原件及历史来源哈希不改。旧清单/patch是历史证据，当前文件哈希以Phase 6为准。

`tests/test_release.py`为7项工作簿依赖检查添加明确的缺失输入skip，另2项既有raw/cache条件保留；全部原断言、预期值和正式参数保持。没有为了通过而替换答案。

41个原正式/扩展模块中40份字节不变。`problem4_final_paper_results.py`仅删除一处字符串外的行尾空格，完整AST与来源一致，输出字符串未变；41份模块AST全部一致。运行器、路径管理、requirements及正式配置文件均保持原字节。没有模型、约束、目标、搜索算子或迭代参数变更。

## 3. 本轮实际环境与命令

本轮新环境：Windows / Python **3.9.10**，`.venv/pyvenv.cfg`为 `include-system-site-packages=false`；用户site关闭，全部核验包位于新venv。没有修改系统Python、全局包或旧开发环境。本轮安装成功，没有沿用Phase 5的成功状态或代理失败记录；未修改系统代理。

| 项目 | 实际结果 |
|---|---|
| venv创建 | exit 0，39.293 s |
| requirements主流程安装 | exit 0，378.098 s |
| requirements-experiments可选安装 | exit 0，59.746 s |
| pip check | exit 0，`No broken requirements found.` |
| 49份Python语法 | Python 3.9 AST解析通过 |
| `run.py --list` | 项目外CWD实际执行，exit 0，41入口，0.302 s |
| 合成示例 | 项目外CWD实际执行，exit 0，2份资源，27.072 s（含首次导入/缓存） |
| unittest discover | exit 0；24项，**15通过、9跳过、0失败、0错误**；unittest计时44.737 s，总45.583 s |
| 41原模块导入 | 在上述测试中实际完成；可选OR-Tools已安装，不是仅解析语法 |
| 白名单验证器 | 默认文件验证和`--check-index`最终均通过；无范围外文件或索引字节差异 |
| 文档 | 相对链接、PowerShell示例语法、Mermaid和KaTeX解析均通过；精确计数见验收JSON |

直接依赖版本：NumPy 2.0.2、Pandas 2.3.3、Matplotlib 3.9.4、OpenPyXL 3.1.5、Rasterio 1.4.3、PyProj 3.6.1、GurobiPy 12.0.3、NetworkX 3.2.1、Affine 3.0.1、Pillow 11.3.0；可选OR-Tools 9.15.6755。源码没有SciPy依赖。原requirements未经升级。

实际命令的可读等价形式如下；入口列举与示例还使用完整脚本路径从项目外CWD执行。执行子进程清除了UAV_*输入覆盖变量，未向公开测试提供旧项目路径。新建venv使用已有3.9.10解释器，下面创建命令仅供环境尚不存在时使用：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -B -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -B -m pip install -r requirements-experiments.txt
.\.venv\Scripts\python.exe -B -m pip check
.\.venv\Scripts\python.exe -B run.py --list
.\.venv\Scripts\python.exe -B scripts/demo_public_intervals.py
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -B scripts/verify_public_release.py
.\.venv\Scripts\python.exe -B scripts/verify_public_release.py --check-index
```

文档审计首次在默认执行限制下未能加载PowerShell解析脚本，记为失败尝试；随后经授权在限制外运行同一检查成功。只解析代码块，没有执行其中的commit/remote/push，也没有更改系统执行策略。Mermaid/KaTeX使用已有本地工具只读加载，其输出仅写入新副本审计目录。

本轮没有调用Gurobi optimize或新建正式模型；真实Q1模型检查因缺少冻结工作簿而跳过。包安装/导入成功不等于完整规模许可已核验。许可证与凭据不在发布集合中。

## 4. 通过与跳过的含义

15项通过由原测试中6项无私有输入检查、6项发布保护检查、新增3项区间示例检查组成。保护检查覆盖缺失输入错误、已有run拒绝覆盖、输出越界、mock reference只读、raw只读和未知pickle在反序列化之前拒绝。测试fixture仅为保护机制用字节，不冒充实验数据。

9项跳过分别涉及：36份正式文件哈希；真实航段/提交表路径；真实reference写保护；追加工作簿复制；Q1/Q2/Q3正式结构；真实Q1建模；Q4归档报告；外部raw/Q4资源入口；可信正式缓存身份。它们没有在公开副本中获得重新验证，不能将24项全部记作通过。Phase 5曾在带授权材料的环境中15＋6项全通过，是不同范围的历史证据。

合成示例固定A=[0,4]、B=[1,3]、C=[3,5]、D=[5,7]秒，调用原 `interval_partition`，实际返回2份资源；不移动时刻、分配无重叠。另验证空集0份资源、3个重叠区间3份资源。输出明确 `competition_result=false`，不是正式80箱结果，也不证明完整调度或通信可行性。

## 5. 正式结果与原件保护

本轮重新核验原始1745文件的集合、SHA、大小、mtime及mode；原候选96个白名单文件和70个隔离文件的内容及属性保持一致，包含36份正式reference。另核对原候选完整20837文件树的文件集合与大小/mtime/mode，没有新增、删除或改变。此处是只读完整性验证，不是重新计算1008项资源或优化目标。

README的18批、20架次、能耗、时间、通信配置和Pareto数值继续引用真实归档的既有指标清单，未编造或重新生成正式结果。Phase 5的41项归档和1008项资源计数复核没有冒充Phase 6的新结果。

Q1时间为累计作业，Q2/Q3为最大完工；Q3零未覆盖属于1s采样及检测到的边界0.01s细化，非任意连续时刻解析证明；Q4固定Q3时序的分区资源配置不代表缩短完工时间。

## 6. Git、安全与授权结论

本地仓库分支为main，最终索引106文件与显式白名单完全相同，逐blob与工作文件字节一致；`git status --short`仅显示白名单内新增文件，`git diff --check`及`git diff --cached --check`均exit 0。`.gitignore`探针与真实索引分别核查。没有commit，没有remote，没有账号/token写入，没有远程网络命令。

已授权的源码归属依据是用户明确确认全部本人编写。原创文档、合成示例与工程工具不引入外部素材。未发现源码中需另行授权的外来实现；不将安装到本地venv的第三方库当作可随项目再授权的内容。第三方依赖没有vendoring，也没有纳入Git索引。

本轮不代作者选择许可证。公开展示自有源码与授予复用许可是两回事；若后续需要MIT/Apache等许可，作者应先决定授权范围，并核对实际分发内容与所用第三方组件条款，不能将第三方数据、Gurobi软件或未来图片一并自动授权。当前无LICENSE作为已披露限制，不是源码作者身份未确认。

安全扫描的范围、误报处理、权限限制和原材料隔离见[安全报告](phase6_security_report.md)。发布集合不含工作簿、DEM、论文、图像、pickle、日志、私钥、Token或个人路径；`.venv/`、`.release-audit/`、测试缓存只在本地忽略区，不能把整个工作目录压缩上传。

## 7. 剩余限制与后续操作

- 无公开真实输入/冻结工作簿，不能从公开克隆独立完成四问题实验。数据授权、图片及论文再分发继续单独审核。
- 完整MILP、ALNS/VNS、通信缓存重建、完整Q4主流程和跨平台验证尚未执行；没有算法性能提升的新结论。
- 固定基准、权重口径、扩展缓存和新旧输入回退限制继续保留；见[已知问题](known_issues.md)。
- GitHub线上Markdown/Mermaid渲染及Actions未执行。本轮只做本地解析，无新增CI工作流。
- 最终提交署名/邮箱、GitHub账号、仓库地址、可见性和是否授予复用许可证由作者确认。已准备好本地暂存内容，人工操作见[发布清单](phase6_github_release_checklist.md)。

Phase 6到此停止。READY允许进入人工确认，不等于已经发布或批准整包材料公开。
