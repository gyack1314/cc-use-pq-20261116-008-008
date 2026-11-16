# 科技人才线索采集枢纽

本地离线运行的科技人才供需线索采集器。接收来源登记文件与镜像批次，支持 JSON、CSV 和简单 HTML 表格，将岗位、培养计划和校企合作项目统一为可追溯记录。SQLite 保存批次、页、原始记录、版本和事件；当前视图只交付有效且无争议的最新线索。

## 快速运行

需要 Python 3.11 或更新版本。项目只使用标准库。

    python -m unittest discover -s tests -v
    python -m compileall -q talent_harvest
    python -m talent_harvest demo --workspace fixtures

演示命令使用临时数据库，不改动夹具。实际使用时先登记来源，再采集镜像：

    python -m talent_harvest --db harvest.sqlite3 register fixtures/source.json
    python -m talent_harvest --db harvest.sqlite3 ingest fixtures/manifest.json
    python -m talent_harvest --db harvest.sqlite3 current
    python -m talent_harvest --db harvest.sqlite3 audit

ingest 对失败页返回非零退出码，并将原因保存在数据库。修复本地镜像文件后，重新运行同一批次即可只重试未完成页；ingest --page N 可指定页。已登记批次的清单不可修改；需要新声明时使用新批次标识。撤回来源使用 withdraw SOURCE --reason TEXT，这会让来源记录退出当前视图，保留历史与审计事件。

## 优先规则

register-rule RULE_JSON 登记官方来源优先规则：key、source、kind、regions、valid_from、valid_until（YYYY-MM-DD，闭区间）与可选 note。优先范围只覆盖所列类别、地区与有效期间；规则指向的来源必须先登记且未撤回，kind 须与来源一致。

- 登记时若与另一条生效规则的地区与有效期重叠且指定来源不同，属于无法唯一裁定的重叠，登记被拒收（退出码 2），拒收原因写入 rule_rejections 并可用 rules 命令查询；同一来源或范围不相交（地区或有效期不重叠）的规则可以并存。
- 以相同 key 再次登记不同内容会产生新修订，旧修订保留用于历史核对；相同内容重复登记是幂等的。
- 每个采入完成的页保存当时生效的规则版本依据（pages.rules_basis，可在 status 与 batch-evidence 中查看）；之后的规则修订不会改写已完成页的语义，补页按各自完成时的版本记录。
- 同一实体数量冲突时，只有在适用范围内、质量合格且未撤回的权威声明才用于现行供需，其余声明作为异议保留在交付实体的 objections 与 trace、conflicts 报告中；范围之外的相反声明仍按现有规则列为冲突。规则修正、补页、质量隔离与来源撤回会产生带有规则版本依据（current 输出的 rules_basis 与实体的 authority）的新判断，已导出的快照保持原解释。

## 输入契约

来源 JSON 包含 key、name、format（json、csv 或 html）、kind（position、program 或 partnership）、可选 field_map 与 trust（0–100）。field_map 的键是规范字段名，值是镜像中的字段名。

批次清单 JSON 包含 source、batch、带时区的 captured_at，以及顺序从 1 开始的 pages。每页包含相对 path 和对文件原始字节计算的 SHA-256。文件须位于清单目录下，UTF-8 编码，每页最多 2 MB，最多 10,000 条记录。JSON 页可用记录数组或 records 对象字段；CSV 的第一行为表头；HTML 页使用一个简单表格的首行作为表头。

每条记录需要 record_id、organization、title、region、discipline、quantity、published_at（YYYY-MM-DD）；reference 与 description 可选。数量为非负整数，零数量、未来发布日期、低可信来源和缺少明确引用的合作项目会被隔离。基础实体键由类别、机构、标题和地区确定；相同实体的不同来源若数量、学科或发布日期不一致，会列入争议而非当前可用数据。实体键是精确规范化后的匹配，不做模糊别名判断。

## 查询与运维

- current --quarantine 返回有效实体、争议与隔离记录；默认不附带隔离明细。
- rules 列出优先规则全部修订（active/superseded）与拒收记录。
- history、trace IDENTITY、record SOURCE ID、timeline SOURCE、batch-evidence SOURCE BATCH 追踪来源与修订。
- status SOURCE BATCH、operations、recovery、quality、audit 检查页级恢复和数据状态。
- summary、distribution、coverage、age YYYY-MM-DD、trend 和 conflicts 提供当前与历史摘要。
- snapshot FILE 写出当前视图与摘要；verify FILE SHA256 校验交付字节。
- preview SOURCE_JSON MANIFEST 在不登记批次的情况下检查本地文件；replay-check MANIFEST 对已完成批次复核原始镜像；checkpoint 读取游标快照。

CLI 将结果作为单行 JSON 写入标准输出，将合同错误写入标准错误。仓库中的机构、岗位和数量均为本地合成演示数据。
