#!/usr/bin/env python3
"""模块 B：PDF 全文（B1 期）。

订阅平台钩子，向论文问答注入全文摘录；由插件 on_enable 调用注册。
"""


def register_fulltext_hooks():
    """订阅平台钩子。由插件 on_enable（迁移成功后）调用。

    幂等性：HookRegistry.add_filter 对同一 (hook, identifier) 自动去重，
    这里仍以 has_filter 先查一次，避免无谓调用。
    """
    from plugin_manager.hooks import get_hook_registry
    from .qa_ext import inject_fulltext_context
    hooks = get_hook_registry()
    if not hooks.has_filter('veroscholar.qa_context', inject_fulltext_context):
        hooks.add_filter('veroscholar.qa_context', inject_fulltext_context,
                         identifier='veroscholar_fulltext')
