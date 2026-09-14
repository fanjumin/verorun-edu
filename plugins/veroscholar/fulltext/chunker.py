#!/usr/bin/env python3
"""模块 B：段落分块 + 问答相关性打分（B1 词面重叠，B2 升级向量）。"""
import re

CHUNK_MAX = 1200   # 单块字符上限


def chunk_pages(pages: list) -> list:
    """按段落切块（块 ≤ CHUNK_MAX 字符，追加前检查边界），记录起始页码。

    例外：单段落本身超过 CHUNK_MAX 时，该段落独立成块（不硬切断句子）。
    """
    chunks, buf, start_page, size = [], [], None, 0
    for p in pages:
        for para in p['text'].split('\n'):
            para = para.strip()
            if not para:
                continue
            if buf and size + len(para) > CHUNK_MAX:      # 追加前检查，先落块
                chunks.append({'page': start_page, 'content': '\n'.join(buf)})
                buf, size, start_page = [], 0, None
            if start_page is None:
                start_page = p['page']
            buf.append(para)
            size += len(para)
    if buf:
        chunks.append({'page': start_page or 1, 'content': '\n'.join(buf)})
    return chunks


def top_relevant_chunks(chunks: list, question: str, k=3) -> list:
    """词面重叠打分取 top-k（中英混合：\w 对 CJK 按字匹配，MVP 足够）。"""
    q = set(re.findall(r'\w+', (question or '').lower()))
    if not q or not chunks:
        return chunks[:k]

    def score(c):
        t = set(re.findall(r'\w+', c['content'].lower()))
        return len(q & t) / len(q)

    return sorted(chunks, key=score, reverse=True)[:k]
