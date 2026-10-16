# Phase 2 实施报告

审查日期为2026年10月15日（Asia/Shanghai）。新建独立发布候选目录，原竞赛目录保持不变。未修改算法源码、未重新生成正式结果、未运行优化、未读取pickle、未初始化Git或上传。

## 文件冻结

复制 **112 个文件**，合计 **43.78 MiB**，源与副本SHA-256全部一致。其中41份非空自有Python源码保持原字节：src 22份、experiments 11份、scripts 8份；另复制原requirements。

正式冻结区 results/reference 共 **36 个文件**，已设置本地只读属性，包括问题一3表、问题二1表、问题三6表、问题四15表和10份最终LaTeX表，以及综合提交表1份。问题三权重后评价表放入experiments，与正式VNS结果分开。

原目录 **1745 个文件**的文件集合、SHA-256、大小、修改时间和权限模式全部与操作前快照一致。复制验收112/112通过。Windows只读属性仅用于本地保护，不是跨平台Git权限保证。

完整复制清单：[provenance.csv](provenance.csv)。逐一排除原因：[excluded_files.csv](excluded_files.csv)。机读验收：[phase2_verification.json](phase2_verification.json)。生成文件另列于generated_files.csv，未伪造历史来源。

## 正式版本

- 问题一：航段预处理、最大安全载荷、集合划分及开发目录正式三表。
- 问题二：problem2_final_run_and_visualization.py；静态读取确认1000次、20260927、权重(0.55,0.15,0.15,0.15)，相关源码和正式结果原哈希已锁定。1500次包装器归扩展实验。
- 问题三：采用根附件干净VNS表，实读20条有效运输架次。联合评价、通信、ALNS、VNS源码均原样保留；覆盖表、精英池、基线和ALNS结果已冻结。缓存及manifest仅本地隔离。
- 问题四：完整枚举、区间资源核算、Pareto与最终分析代码均保留；最终方案K2-032/K3-007。旧报告函数及工作簿仍是实际依赖，注明历史辅助用途，不能误认为最终方案。

## 依赖与缺口

[dependencies.csv](dependencies.csv)为机读依赖表，[module_imports.csv](module_imports.csv)记录66条本地导入边。data/processed仅保存映射，避免重复工作簿。

1. 五份原始Excel、DEM和原提交模板在原目录存在，但授权未确认，未复制。因此发布副本目前不能独立求解。
2. 指定的结果提交模板_问题三已填写.xlsx在原目录缺失。现有综合提交表Q3页为215条区间/20个运输架次；104条中继区间与Q4存档逐项对应，时间最大差约5.0e-12秒。这支持后续候选映射，不表示不存在的原文件与提交表字节相同。本阶段没有重命名、生成别名或提取重写区间。
3. Q3 posthoc所需已完成100轮case1结果已保留；整组权重重求解未完成。可选中断日志另放本地审核区，避免丢失审计计数来源。
4. 源码的中文目录、裸导入、sys.path、CWD路径和固定输出位置仍未适配。results/runs只是明确的后续输出位置，尚未接入算法。
5. Gurobi许可未测试；NetworkX原本依赖本地vendor，已排除vendor；OR-Tools为未列入现有requirements的扩展依赖。

## 公开范围

原始数据、DEM、第三方地图、论文及合作队员文档没有复制。正式结果含原始数据衍生内容，部分工作簿含个人元数据和本机路径；为保持原哈希，这些内容未修改，全部冻结结果、实验结果及review_hold已被.gitignore隔离。

本目录是本地候选，**不是已获准公开的完整目录**。不得压缩整个目录上传；Git忽略不能阻止人工压缩。审核清单见review_required.csv，公开门槛见publication_gate.json。没有添加LICENSE。

## Phase 3

按[工作清单](phase3_worklist.md)完成路径与导入适配、只读reference/runs输出隔离、模板输入映射、缓存兼容与依赖安装说明。算法口径问题另行处理，不改原正式结果。完整复现和长时间优化不属于本阶段验收。
