# VeroScholar 优化与改名方案（备查文档）

> 创建日期：2026-08-31
> 状态：**阶段 0 / 1A / 1B / 1C 已完成**（阶段 2 待执行）
> 适用范围：`plugins/veroscholar/`（插件完全自包含，不涉及系统核心）

---

## 1. 背景与目标

VeroScholar 为 VeroRun AI 教育版「科研全流程」插件（v1.2.3），覆盖**选题 · 文献 · 写作 · 审稿**。本文档回答两个问题：

1. **按功能重新定名**：现中文名「科研工作台」偏静态工具感，与 SciSpace「科研空间/工作台」同质化，未体现「AI 智能体 + 全流程」核心差异。
2. **功能对齐评估**：对照主流 AI 科研平台（Elicit / Consensus / Scite / SciSpace / Research Rabbit / NotebookLM / Paperguide / Zotero 等），评估可落地的提升项及实施顺序。

---

## 2. 现状盘点（代码实证）

| 模块 | 实现位置 | 说明 |
|------|---------|------|
| 多源文献检索 | `adapters/` | arXiv / Semantic Scholar / OpenAlex 三源聚合，DOI + title+year 去重 |
| 论文知识库 | `models.py` | 元数据入库、检索、筛选、按被引数排序 |
| 研究项目 | `models.py` | CRUD + team_members + 论文归类 |
| 阅读笔记 | `models.py` | 4 类笔记（总结/批判/方法/疑问）+ pgvector 语义向量（后台线程） |
| 综述生成 DAG | `workflow.py` + `workflows/literature_review.json` | 关键词扩展 → 多库检索 → 去重排序 → 方法分类 → 生成综述 → 通知 |
| 3 个子 Agent | `agents/` | Literature Review / Experiment Designer / Paper Writer（Prompt 级） |
| 系统能力 | `plugin.json` | JWT 鉴权、iframe 页面、i18n 双语、健康检查、仪表盘统计、33 个测试用例 |

**可复用基建（已核实）**：
- LLM：`agent_matrix.engine.UnifiedLLM.chat()` / `get_embedding()`
- Embedding：`plugins/_base/embeddings.py`（EmbeddingService，模块级配置）
- 工作流：orchestrator `engine.run_workflow()` + `notify` 节点
- 调度：vault `services/scheduler.py`（阶段 2 订阅提醒可复用）

---

## 3. 主流平台功能对比

2026 年行业共识：AI 科研工具的分水岭已从「能否生成」转向 **「引用是否真实可审计」**（Nature 2025 调查：65% 研究者接受 AI 起草；审稿人普遍对虚构 DOI 执行引用审计）。

| 能力域 | 代表产品 | VeroScholar |
|--------|---------|:-----------:|
| 多源学术检索 | Elicit / Consensus / SciSpace（2 亿+文献） | ✅ 已有（3 源，元数据级） |
| 摘要 / 综述生成 | Consensus / Elicit / SciSpace | ✅ 已有（DAG 综述） |
| **引用真实性校验（防幻觉）** | Paperguide / Elicit | ⚠️ 仅提示词约束，无校验层 |
| **Chat-with-Paper 问答** | SciSpace / NotebookLM | ❌ |
| **PDF 上传 + 全文解析** | Elicit / SciSpace / NotebookLM / Zotero | ❌（仅 pdf_url 外链） |
| **结构化信息抽取表** | Elicit（自定义列） | ❌ |
| **引文语境分析** | Scite（supporting/contrasting/mentioning） | ❌ |
| **引文网络可视化** | Research Rabbit / Connected Papers | ❌ |
| **引文导出（BibTeX/RIS/CSV）** | Elicit / Zotero / Paperguide | ❌ |
| **保存检索 / 订阅提醒** | Elicit Notebook / Scopus | ❌ |
| 高级过滤（期刊/引用区间） | Scopus / Web of Science | ⚠️ 仅 source/year/关键词 |
| 相关论文推荐 | Research Rabbit / Semantic Scholar | ❌ |
| 笔记 + 双向链接知识管理 | Obsidian / Zotero | ⚠️ 仅单论文笔记 |
| 全文检索 | Zotero / 各数据库 | ❌（仅 title/abstract ILIKE） |
| 论文互译 | SciSpace / DeepL / Paperpal | ❌ |
| 写作端到端（大纲→初稿→润色→引用） | 沁言学术 / Paperguide / Jenni | ⚠️ 仅 4 章节 Prompt |
| 系统评价（PRISMA/筛查/偏倚） | Elicit Pro / RevMan | ❌ |
| 实验数据分析 | NotebookLM / Code Interpreter | ❌ |
| 协作（共享笔记/权限） | Elicit Team / Zotero | ⚠️ 仅 team_members |

---

## 4. 命名方案（定案）

**正式中文名：引源索骥**
**副标题：文献溯源 · 综述创作 · 科研全流程 AI 助手**
**英文名不变：VeroScholar**

**理由**（2026-08-31 与用户讨论定案）：
- 「引源索骥」= 引用文献、溯源求实、按图索骥，精准体现「文献溯源检索引用」这一最强差异点；
- 用户否决「研智」（太抽象，不像插件名，用户无感）；
- 「综述创作」能力由副标题补齐（命名承载品牌意象，功能由 tagline 说明，行业惯例）；
- tagline 沿用「从选题到投稿，AI 赋能科研全流程」。

**弃用**：「研智」（抽象，用户否决）、「研途」（与考研机构撞名）、「智研工作台」（仍偏工具感）、「维洛学术」（丢失功能描述）、「科研智伴」（偏消费化）。

---

## 5. 功能对齐可行性矩阵

| # | 提升项 | 对齐判定 | 依赖 | 工作量 | 阶段 |
|---|--------|:--------:|------|:------:|:----:|
| 1 | 引用落库校验（防幻觉） | ✅ 全对齐 | `get_by_doi` + papers 表 DOI | 小 | 1A |
| 2 | BibTeX / RIS 引文导出 | ✅ 全对齐 | 数据已齐 | 小 | 1A |
| 3 | 论文问答 RAG | ✅ 全对齐 | EmbeddingService + UnifiedLLM.chat | 中 | 1B |
| 12 | 论文翻译（中英） | ✅ 全对齐 | UnifiedLLM.chat | 小 | 1B |
| 8 | 全文检索 | ✅ 全对齐 | pg_trgm 扩展 + GIN 索引 | 小-中 | 1C |
| 9 | 标签 / 阅读状态 / 双向链接 | ✅ 全对齐 | schema 扩展 | 小 | 1C |
| 7 | 相关论文推荐 | ⚠️ 部分（向量推荐可先落地；引文图需引用关系数据） | 摘要 embedding | 中 | 1B |
| 4 | PDF 上传 + 全文解析 | ⚠️ 部分（需 pypdf 依赖 + 存储，涉 install.sh） | 大 | 2 |
| 6 | 保存检索 + 订阅提醒 | ⚠️ 部分（复用 scheduler + notify） | 中 | 2 |
| 5 | 结构化信息抽取表 | ⚠️ 延后 | UnifiedLLM.chat JSON + jsonb | 中 | 2 |
| 10 | 引文图可视化 | ❌ 暂缓 | 需引用关系数据采集 | 大 | 2 |
| 11 | PRISMA 系统评价 | ❌ 暂缓 | 超出教育版定位 | 大 | — |
| 13 | 协作权限 | ❌ 暂缓 | 系统为单管理端管理员体系 | 大 | — |
| 14 | 数据分析节点 | ❌ 暂缓 | 需代码执行沙箱 | 大 | — |

**结论：14 项中 8 项可全对齐，3 项部分对齐（2 项延后），4 项暂缓。**

---

## 6. 分期实施计划

| 阶段 | 内容 | 依赖风险 |
|------|------|---------|
| 阶段 0 | 重命名「研智」（纯文案） | 低 |
| 阶段 1A | 引用校验 + 引文导出 | 低 |
| 阶段 1B | 问答 RAG + 翻译 + 相关推荐（共用 1 个迁移：papers 加 embedding 列） | 中（LLM key 缺失降级） |
| 阶段 1C | 全文检索 + 标签 / 阅读状态 | 中 |
| 阶段 2 | 保存检索订阅 / 结构化抽取表 / PDF 上传 / 引文图 | 高（逐项单独批准） |

---

## 7. Edit 清单

### 阶段 0：重命名「引源索骥」（✅ 已完成 2026-08-31）

> 模板中源字符串 `VeroScholar Research Workbench` **不修改**，仅改 zh 映射值 → 中英 UI 各自正确。
> 注意：同一文件的多个 Edit 必须串行执行，并行编辑会互相覆盖（本次 zh-CN.yml 曾因此丢失一次 replace_all）。

| 文件 | 改动 |
|------|------|
| `i18n/zh-CN.yml` | L2 `plugin.veroscholar.name` → `"引源索骥"`；L4 `menu.veroscholar.dashboard` → `"引源索骥"`；L7 `menu.veroscholar` → `"引源索骥"`；L14 → `"引源索骥"`；L15 → `"文献溯源 · 综述创作 · 科研全流程 AI 助手"` |
| `README.md` | L1 → `# VeroScholar — 引源索骥` |
| `README.en.md` | L1 → `# VeroScholar`（该文件由发布流程接管，仅本地一致） |
| `__init__.py` | L2/L62 注释 → 引源索骥（L38 英文 description 保持不动） |
| `routes.py` | L2/L6/L183 注释 |
| `migrations/v1.0.0_init.sql` | L2 注释 |
| `static/css/veroscholar.css` | L1 注释 |
| `static/js/veroscholar.js` | L2 注释 |

> `plugin.json` 英文品牌名不动（版本交发布流程管理）；`i18n/en.yml` 英文 UI 不动；CHANGELOG 历史记录保留原文。

### 阶段 1A：引用校验 + 引文导出（✅ 已完成 2026-08-31，版本 1.3.0）

> 实施偏差：原方案「DAG 内追加校验节点」经核实后放弃 —— 综述 DAG 生成的正文仅存于工作流实例 `node_outputs`，未写回 `reviews` 表，DAG 校验节点对"LLM 编造 DOI"无约束力；改为**端点式校验**（对 reviews.content 做 DOI 反查），价值更高且不动工作流。

| 文件 | 改动 |
|------|------|
| `workflow.py` | 新增 `verify_citations(conn, review)`：正则提取 DOI（兼容 doi.org 前缀）→ 反查 papers 表；paper_ids 逐条反查 |
| `models.py` | 新增纯函数 `to_bibtex(paper)` / `to_ris(paper)`（含作者解析、BibTeX 转义、引用键生成） |
| `routes.py` | 新增 `GET /api/v1/reviews/<id>/verify`、`GET /api/v1/papers/<id>/export?format=bib\|ris` |
| `search.html` | 详情抽屉加「BibTeX」「RIS」导出按钮（`[data-export]`） |
| `review.html` | 综述详情加载时展示引用校验摘要（未验证数 > 0 标红） |
| `veroscholar.js` | 新增 `VS.download(url, filename)` 带鉴权 blob 下载 |
| `i18n/*.yml` | 新增 `verified_references` / `unverified_references` |
| `plugin.json` | 版本 1.3.0；capabilities 新增 `citation.verify` / `citation.export` |
| `CHANGELOG.md` / `README.md` | 功能与用例数（41）同步 |
| `tests/test_routes.py` | 新增 8 用例（verify ×2、export ×6），全部通过 |

### 阶段 1B：问答 RAG + 翻译 + 相关推荐（待执行）

| 文件 | 改动 |
|------|------|
| `migrations/v1.1.0_ai.sql` | 新增：papers 加 `embedding vector` 列（惰性回填） |
| `routes.py` | 新增 `POST /api/v1/papers/<id>/chat`、`POST /api/v1/papers/<id>/translate`、`GET /api/v1/papers/<id>/related` |
| 新增 `services/ai.py` | 封装 `UnifiedLLM.chat()` + EmbeddingService 检索（LLM key 缺失返回 503 降级） |
| `templates/search.html` + `veroscholar.js` | 详情抽屉内嵌「问论文 / 翻译 / 相关论文」UI |

> `services/` 为插件标准结构（vault / visitor_profile 同款），不涉系统核心。

### 阶段 1C：全文检索 + 标签 / 阅读状态（✅ 已完成 2026-08-31，版本 1.5.0）

> 实施要点：
> - 全文检索**不改 SQL 语义**：pg_trgm GIN 索引（title/abstract）自动加速既有 `ILIKE '%q%'`，中文不依赖分词器；
> - 迁移 `v1.1.1_lib.sql`：`pg_trgm` 显式 `SCHEMA public`（与 pgvector 同先例）、`papers.reading_status`、`tags` + `paper_tags` 两表（全幂等）；
> - `set_reading_status` 校验枚举，非法值 400；`add_tag` 用 `ON CONFLICT (name) DO NOTHING` 幂等；
> - 前端：库卡片状态徽标（veroscholar.js `statusBadge`）、抽屉状态选择 + 标签芯片、库工具栏标签/状态筛选下拉。

| 文件 | 改动 |
|------|------|
| `migrations/v1.1.1_lib.sql` | pg_trgm 扩展 + reading_status 列 + 两个 GIN trgm 索引 + tags/paper_tags 表 |
| `models.py` | `list_papers`/`count_papers` 增 tag_id/status 过滤；新增 `list_tags`/`paper_tags`/`add_tag`/`remove_tag`/`set_reading_status` |
| `routes.py` | `api_list_papers` 增 tag/status 参数；新增 `GET /api/v1/tags`、`GET/POST /api/v1/papers/<id>/tags`、`DELETE /api/v1/papers/<id>/tags/<tag_id>`、`PATCH /api/v1/papers/<id>/status` |
| `templates/search.html` | 库工具栏筛选下拉；抽屉状态下拉 + 标签输入/芯片 |
| `static/js/veroscholar.js` | `statusBadge` 徽标渲染 |
| `static/css/veroscholar.css` | `.vs-badge-status` / `.vs-chip` / `.vs-chip-x` |
| `i18n/*.yml` | 状态与标签文案（含 `reading_status_*` 键） |
| `tests/test_routes.py` | 新增 10 用例（标签增查删、状态 400、列表过滤透传） |
| `plugin.json` | 版本 1.5.0 |
| `CHANGELOG.md` / `README.md` | 功能与用例数（57）同步 |

> **文件被外部同步覆盖告警**：1C 执行期间 `README.md` 曾被外部进程还原为旧版本（1B 已提交内容丢失），已用完整版重写恢复。与 plugin.json 版本重置（551f1ed9）同源，建议排查本地自动同步/检查工具。

---

## 8. 风险与对策

| 风险 | 对策 |
|------|------|
| RAG / 翻译依赖 LLM key（dashscope） | 缺 key 一律 503 提示降级，不影响既有功能 |
| 论文级向量全量嵌入成本高 | 惰性嵌入（打开详情 / 存笔记时触发），迁移不强制全量回填 |
| 阶段 2 PDF 解析需新增 `pypdf` 依赖 | 涉 `install.sh`（铁律 3.2），必须单独申报，本方案不包含 |
| 「引源索骥」商标占用 | 上线前查重（产品决策，需用户确认） |
| `to_tsvector` 中文分词差 | 改用 pg_trgm（trigram），不建 tsvector |

---

## 9. 验证方案

- **阶段 0**：`grep -rn "科研工作台" plugins/veroscholar` 除 CHANGELOG 外零命中；页面标题 / 菜单显示「引源索骥」。
- **阶段 1**：跑现有 57 用例 + 新增用例；实测综述工作流校验结果、导出文件格式（BibTeX/RIS）、RAG 问答、检索索引命中、标签/阅读状态 CRUD。

---

## 10. 决策记录

| 决策项 | 结论 | 状态 |
|--------|------|------|
| 中文名 | 定案「引源索骥」（副标题：文献溯源 · 综述创作 · 科研全流程 AI 助手），英文 VeroScholar 不变 | ✅ 已完成（2026-08-31） |
| 阶段 0 | 重命名「引源索骥」全部落地 | ✅ 已完成（2026-08-31） |
| 阶段 1A | 引用校验 + 引文导出 | ✅ 已完成（1.3.0，commit 13cba25e） |
| 阶段 1B | 论文问答 + 翻译 + 相关推荐 | ✅ 已完成（1.4.0，4 个 commit） |
| 阶段 1C | 全文检索 + 标签 + 阅读状态 | ✅ 已完成（1.5.0） |
| 商标查重 | 「引源索骥」需在正式发布前完成查重 | 待办 |

---

*本文档由 2026-08-31 分析会话沉淀，执行前请逐项复核 Edit 清单与当前代码状态。*
