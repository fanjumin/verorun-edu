#!/usr/bin/env python3
"""模块 B：PDF 解析（pypdf，惰性导入——未安装时探测降级）。"""
import io


def pypdf_available() -> bool:
    try:
        import pypdf  # noqa: F401
        return True
    except Exception:
        return False


def parse_pdf(data: bytes) -> list:
    """解析 PDF → [{'page': n, 'text': str}]；失败抛 ValueError。

    i18n 约定（插件标准 §2.1）：ValueError 消息为**固定的英文 i18n 源串字面量**，
    由路由层 _() 包装后返回；禁止拼入动态内容（会破坏「源串即 Key」的查表）。
    """
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception:
        raise ValueError('PDF parse failed')
    pages = []
    for i, page in enumerate(reader.pages):
        text = (page.extract_text() or '').strip()
        if text:
            pages.append({'page': i + 1, 'text': text})
    if not pages:
        raise ValueError('No extractable text found in PDF')
    return pages
