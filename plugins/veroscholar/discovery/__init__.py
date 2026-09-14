#!/usr/bin/env python3
"""VeroScholar Discovery Engine（假设发现引擎，v1.11.0）。

职责：
  1. 科研选题发现工作流（DAG 节点集 + hypothesis_tournament 蓝图）
  2. 假设生命周期（generate → evidence_recall → rank → cluster → discuss
     → judge → persist）的 RESTful API 与数据层
  3. 与主插件共享 veroscholar_bp 蓝图、鉴权与插件配置门禁（discovery_enabled）

模块组织：
  models_discovery  数据层（6 张表：discovery_runs/hypotheses/hypothesis_votes/
                    hypothesis_discussions/discovery_events/discovery_memory）
  discuss           假设讨论协议（Generator/Critic/Evolver/Comparator + Elo）
  nodes             DAG 节点处理器（veroscholar_discovery_* 7 个）
  routes_discovery  5 组 API + /discovery 工作台页面

本包为纯插件代码，100% 位于 plugins/veroscholar/discovery/ 内，不触碰系统核心模块。
"""

from .nodes import get_discovery_dag_nodes
from .routes_discovery import register_discovery_routes

__all__ = ['get_discovery_dag_nodes', 'register_discovery_routes']
