#!/usr/bin/env python3
"""VeroScholar 引源索骥插件。

面向教育版的科研全流程智能助手：多库文献检索（arXiv/Semantic Scholar/OpenAlex）、
项目-论文管理、论文笔记（含语义向量）、DAG 综述生成工作流。

生命周期（对齐插件标准 §12）:
  on_install    → 迁移 veroscholar 独立 schema
  on_enable     → 幂等迁移（重复启用安全）
  register_routes      → veroscholar_bp（页面 + RESTful API）
  register_dag_nodes   → veroscholar_search 自定义节点
  register_health_checks → 数据库连通性健康检查
  get_dashboard_stats  → 管理后台总览卡片统计
  on_uninstall  → DROP SCHEMA veroscholar CASCADE（卸载零残留）
"""

import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

from .routes import veroscholar_bp
from . import models as m
from .workflow import get_dag_nodes

from plugin_manager.base import BasePlugin
from plugin_manager.logger import get_plugin_logger

_logger = get_plugin_logger('veroscholar')


class VeroScholarPlugin(BasePlugin):
    name = 'veroscholar'
    @property
    def version(self):
        info = getattr(self, 'plugin_info', None)
        return getattr(info, 'version', None) or '0.1.0'
    description = 'Research workbench — multi-source literature search, projects, notes and review generation'
    author = 'VeroRun'

    def on_install(self, registry):
        """安装时初始化独立 schema。"""
        try:
            if not m.migrate('0.0.0', self.version):
                _logger.error('veroscholar migration failed during install')
                return False
            _logger.info('veroscholar schema installed')
        except Exception as e:
            _logger.error('install failed: %s', e)
            return False
        return True

    def on_enable(self, registry):
        """启用时执行幂等迁移（表已存在则跳过）。

        迁移失败不再静默 warning：升级为 error 并暴露到服务日志/健康状态，
        避免 schema 陈旧时仅 500 而无任何可见告警。
        迁移成功后注册全文模块钩子（模块 B；失败仅降级不阻断启用）。
        """
        ok = False
        try:
            ok = m.migrate('0.0.0', self.version)
            if not ok:
                _logger.error('veroscholar migration FAILED on enable; schema may be stale')
        except Exception as e:
            _logger.error('enable migration exception: %s', e)
        if ok:
            try:
                from .fulltext import register_fulltext_hooks
                register_fulltext_hooks()
            except Exception as e:
                _logger.warning('fulltext hook registration skipped: %s', e)
            try:
                # 知识库读向：论文问答合并 project_workspace 检索（方案 §6.2）
                from .services.kb_sync import register_kb_hooks
                register_kb_hooks()
            except Exception as e:
                _logger.warning('kb sync hook registration skipped: %s', e)
        return True

    def register_routes(self):
        """注册引源索骥 Blueprint。"""
        return [veroscholar_bp]

    def register_dag_nodes(self):
        """注册自定义工作流节点：veroscholar_search + discovery 节点集。

        discovery 节点（veroscholar_discovery_*）独立于主节点加载，任一
        节点集导入失败仅降级不影响另一集（discovery_enabled 门禁在节点
        入口处判断，关闭时零副作用）。
        """
        nodes = get_dag_nodes()
        try:
            from .discovery import get_discovery_dag_nodes
            nodes.update(get_discovery_dag_nodes())
        except Exception as e:
            _logger.warning('discovery nodes not loaded: %s', e)
        return nodes

    def register_health_checks(self):
        """§12.6 健康检查：veroscholar schema 连通性。"""
        return [{
            'name': 'veroscholar_db',
            'description': 'VeroScholar schema reachable and papers table readable',
            'check': _check_db,
        }]

    def get_dashboard_stats(self) -> dict:
        """§10.5 管理后台总览卡片统计。

        键名必须与 plugin.json dashboard.stats[].key 声明一致（标准 §2.3），
        不附加插件前缀；聚合层按插件 id 分桶。
        """
        try:
            with m.get_db() as conn:
                return m.get_stats(conn)
        except Exception as e:
            _logger.warning('dashboard stats failed: %s', e)
            return {}

    def on_disable(self, registry):
        """停用时无需额外清理（路由由 manager 统一摘除）。"""
        _logger.info('veroscholar disabled')
        return True

    def on_uninstall(self, registry):
        """卸载清理 — 删除 veroscholar schema（标准 §12.5 卸载零残留）。"""
        from plugins._base.db import get_raw_connection
        try:
            raw = get_raw_connection()
            try:
                cur = raw.cursor()
                cur.execute('DROP SCHEMA IF EXISTS veroscholar CASCADE')
                raw.commit()
                cur.close()
            finally:
                raw.close()
            _logger.info('veroscholar schema dropped')
        except Exception as e:
            _logger.error('on_uninstall cleanup failed: %s', e)
        return True


def _check_db() -> dict:
    """数据库连通性健康检查：返回 {'ok': bool, **detail}。"""
    try:
        with m.get_db() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM papers").fetchone()
        return {'ok': True, 'papers_count': int(row['c'])}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


__all__ = ['VeroScholarPlugin', 'veroscholar_bp']
