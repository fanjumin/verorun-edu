# VeroScholar — 科研工作台

VeroScholar 是 VeroRun AI 系统教育版的科研全流程插件，覆盖**选题 · 文献 · 写作 · 审稿**四个核心阶段。

插件严格遵循 [`docs/plugin-standard-v1.5.md`](../../docs/plugin-standard-v1.5.md) 开发：
单库多 Schema（`veroscholar`）、共享连接池、JWT 管理员鉴权、iframe 独立页、
i18n 双语文案、卸载零残留。

## 功能模块

| 模块 | 说明 |
|------|------|
| 多源文献检索 | arXiv / Semantic Scholar / OpenAlex 三库聚合，DOI 去重，`semantic` 与 `semantic_scholar` 别名 |
| 论文知识库 | 论文入库、搜索、详情；无 DOI 时按 title+year 兜底去重 |
| 研究项目 | 项目 CRUD、成员管理、论文归类 |
| 阅读笔记 | 论文级笔记标注，可选 embedding 向量（pgvector）供语义检索 |
| 综述生成器 | 触发 DAG 工作流：关键词扩展 → 多源检索 → 去重排序 → 方法分类 → LLM 生成综述 |
| 3 个子 Agent | Literature Review（high tier）、Experiment Designer（high tier）、Paper Writer（standard tier） |

## 目录结构

```
plugins/veroscholar/
├── __init__.py                  # BasePlugin 生命周期组装
├── models.py                    # 数据层（共享连接池 + search_path 隔离）
├── routes.py                    # Blueprint：3 个页面 + RESTful API + JWT 鉴权
├── workflow.py                  # DAG 节点处理器 + 综述工作流触发
├── plugin.json                  # 插件元数据（agents/menu/dashboard/settings）
├── migrations/v1.0.0_init.sql   # schema 建表（全部 IF NOT EXISTS 幂等）
├── adapters/                    # 数据源适配器（base + arxiv + semantic_scholar + openalex）
├── agents/                      # 3 个子 Agent 提示词
├── workflows/literature_review.json  # 综述 DAG 蓝图
├── templates/                   # dashboard / search / review 三页
├── static/js/veroscholar.js     # 前端逻辑（VS 命名空间）
├── static/css/veroscholar.css   # design-system 变量风格
├── i18n/en.yml + zh-CN.yml      # 英文键双语映射
└── tests/                       # 单元 + 集成测试（33 用例）
```

## 安装与启用

通过系统插件管理页面上传/启用本插件：

1. `python_dependencies.required` 声明 `feedparser`（arXiv 适配器运行时惰性加载）；
2. 启用时自动执行 `migrations/*.sql`（幂等，记录 `schema_version`）；
3. 卸载时 `DROP SCHEMA veroscholar CASCADE` 零残留。

## 配置项

在插件设置页可配置：

| 配置 | 默认 | 说明 |
|------|------|------|
| `semantic_scholar_api_key` | — | 可选，提高语义检索速率限制 |
| `openalex_api_key` | — | 可选，提高 OpenAlex 请求限额 |
| `default_search_limit` | 30 | 默认检索条数（5–100） |
| `arxiv_enabled` | true | arXiv 数据源开关 |
| `semantic_enabled` | true | Semantic Scholar 数据源开关 |
| `openalex_enabled` | true | OpenAlex 数据源开关 |

## 测试

```bash
cd F:\Sites\VeroRun
python -m unittest plugins.veroscholar.tests.test_adapters \
                       plugins.veroscholar.tests.test_models \
                       plugins.veroscholar.tests.test_routes -v
```

33 个用例覆盖：适配器字段映射 / 摘要重建 / 错误传播、数据层 SQL 构造、
路由鉴权（401/302/静态豁免）、页面与 API 端点。

## 依赖

- 运行时：`feedparser`（Python 依赖）、PostgreSQL + `pgvector`（可选，embedding 列）
- 环境：插件标准 v1.5+，`min_app_version: 0.58.0`
