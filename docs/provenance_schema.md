# 溯源清单说明

`provenance.csv` 覆盖全部复制文件，包括源码、正式结果、必要实验结果、原依赖清单和本地隔离材料。`source_path` 相对原工作目录，`release_path` 相对此发布目录，SHA-256 为原文件及发布副本共同的字节哈希。分类、用途和依赖在复制清单中独立记录。

`dependencies.csv` 通过 dependency_id 对应溯源清单的 dependencies，区分本地存在但授权待审、已复制但路径未接入、指定原文件缺失及候选替代。缺失模板那一行的哈希属于候选提交表，不是缺失文件。

`module_imports.csv` 为41份源码的66条本地模块静态导入边，不意味着重排后能导入。`excluded_files.csv` 覆盖未复制的每个原文件；`review_required.csv` 区分已复制但Git隔离与未复制待授权。

本阶段新写的清单、README和报告属于整理产物，不伪造其历史来源；这些文件列于 `generated_files.csv`。`generated_checksums.csv` 校验最终交付的生成文件，但不包含它自身，以避免自引用哈希。

`.release-audit` 为本地审计区，记录全部原文件操作前快照和验收。构建脚本不属于算法源码，不能对已有冻结目录重复执行。所有授权待审材料均未上传；哈希一致不能证明公开许可成立。
