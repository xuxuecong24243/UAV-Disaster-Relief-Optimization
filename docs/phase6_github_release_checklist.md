# Phase 6 人工发布检查清单

状态：**READY（白名单源码/文档/数值摘要）**。这是本地发布准备记录，尚未远程发布。报告见[Phase 6验收](phase6_report.md)，文件范围见[106文件白名单](phase6_release_manifest.csv)。

## 已完成的本地门禁

| 必须项 | 本轮结果 |
|---|---|
| 从Phase 5白名单核验后逐文件复制 | 96/96来源与复制哈希一致，无整目录复制 |
| 原始材料和旧候选保持不变 | 1745原始文件、166份关键候选文件及20837候选文件元数据复核一致 |
| 原有算法语义/正式参数不改 | 41模块AST相同；一处行尾空格清理已记录；无优化实验 |
| 无私有数据独立环境安装 | Python 3.9.10，新venv，主流程及可选依赖成功，pip check通过 |
| 公开基础功能和测试 | 41入口/模块，合成示例2资源；24测试中15通过、9私有依赖skip、0失败/错误 |
| README与文档 | 当前/历史证据分开，链接和代码块解析有效，无未经授权图片 |
| 安全与授权范围 | 无禁止材料或凭据；无LICENSE，不将第三方依赖/数据自动授权 |
| 最终Git索引 | 106文件与白名单集合、字节一致；diff检查通过，无范围外文件 |
| 远程与提交 | 无remote、无commit、无push；main为未提交分支 |

9项skip没有复现成功；完整优化、真实输入再分发、Gurobi完整规模许可及跨平台/线上渲染仍未验证。没有私有输入并不阻止已明确限定的源码展示，但不能将其宣传为完整竞赛实验可从公开仓库复现。

## 由作者最终确认

- 确认本次公开内容限定在清单内，不打包`.venv`、`.release-audit`、运行输出或原始竞赛目录。
- 确认提交的公开署名与邮箱；建议使用自己GitHub设置中真实、已确认的noreply邮箱，不使用未经替换的占位文本。
- 决定GitHub仓库名称、所属账号、可见性；确认同名远程仓库为空。若已有内容，不直接推送合并，更不能force push。
- 源码作者已确认，无需再次索取共同作者授权。是否采用复用许可证另行决定；未选择时保留README说明，不自行添加LICENSE。
- 未经单独审核不加入DEM、Excel、论文、模板、原图片或缓存。任何额外文件均须更新白名单并重新审查。

## Windows PowerShell 人工操作

以下命令**仅供人工确认后执行**；本阶段没有执行其中的署名配置、commit、remote add或push。先在父目录进入公开副本，已有`.venv`可使用；若是新克隆则先按README安装。所有占位符都必须替换为自己的确认值。

```powershell
Set-Location '.\UAV-Disaster-Relief-Optimization-public'
# 本地验收：必须exit 0，并检查实际暂存内容
.\.venv\Scripts\python.exe -B scripts/verify_public_release.py --check-index
git status --short
git diff --check
git diff --cached --check
git diff --cached --stat

# 确认要公开的署名；只设置此仓库，不使用--global
$publicName = 'YOUR_PUBLIC_NAME'
$publicEmail = 'YOUR_VERIFIED_NOREPLY_EMAIL'
git config user.name $publicName
git config user.email $publicEmail

# 文件已按白名单暂存；不要使用git add .
git commit -m 'Prepare public UAV optimization project'
```

提交后，在GitHub网页手工创建**空仓库**，不自动初始化README、LICENSE或.gitignore，并自行确认可见性。只有完成这一步并确认仓库URL之后，再执行下面的远程操作：

```powershell
# 用本人实际确认的账号和仓库URL替换；不要原样执行占位URL
$remoteUrl = 'https://github.com/YOUR_GITHUB_OWNER/UAV-Disaster-Relief-Optimization.git'
git remote add origin $remoteUrl
git remote -v
git push -u origin main
```

本地发布验证器的`--check-index`模式还要求无remote，用于本阶段的零远程准备门禁；在你主动配置remote后，使用不带该参数的文件白名单检查，并人工审查实际仓库地址。若上传前修改README或其他文件，旧哈希清单将报错，应重新审核并更新快照，不能关闭验证器或修改预期值来掩盖变化。

发布后可人工检查GitHub页面、Mermaid和代码块渲染；本阶段没有代执行此步骤，也没有使用任何自动创建仓库或修改可见性的命令。
