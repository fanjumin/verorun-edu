#!/usr/bin/env python3
"""模块 B：全文上传/浏览路由。注册到既有 veroscholar_bp——
自动继承其 before_request JWT 鉴权与 paper_id uuid 前置校验（蓝图行为）。

幂等：同文（sha256）重复上传返回既有记录（added=0），不重复解析/灌块。
"""
import hashlib

from flask import request, jsonify

MAX_UPLOAD_BYTES = 10 * 1024 * 1024   # 10MB


def register_fulltext_routes(bp):
    from i18n import _                       # 插件标准：用户可见文案必须 _() 包装
    from .. import models as m
    from ..routes import _current_user_id
    from . import parser, chunker

    @bp.route('/api/v1/papers/<paper_id>/fulltext', methods=['POST'])
    def api_upload_fulltext(paper_id):
        """上传 PDF → 解析 → 分块入库。幂等：同文（sha256）重复上传返回既有记录。"""
        if not parser.pypdf_available():
            return jsonify({'success': False,
                            'error': _('PDF parsing is not available (pypdf missing)')}), 503
        f = request.files.get('file')
        if not f or not (f.filename or '').lower().endswith('.pdf'):
            return jsonify({'success': False, 'error': _('A PDF file is required')}), 400
        data = f.read()
        if len(data) > MAX_UPLOAD_BYTES:
            return jsonify({'success': False, 'error': _('File too large (max 10 MB)')}), 413
        try:
            with m.get_db() as conn:
                if not m.get_paper(conn, paper_id):
                    return jsonify({'success': False, 'error': _('Paper not found')}), 404
                sha = hashlib.sha256(data).hexdigest()
                # 幂等：同文（sha256）已解析过 → 直接返回既有记录，不重复解析/灌块
                existing = conn.execute(
                    "SELECT id, n_chunks FROM fulltext_files "
                    "WHERE paper_id = ? AND sha256 = ? LIMIT 1",
                    (paper_id, sha)).fetchone()
                if existing:
                    return jsonify({'success': True, 'data': {
                        'file_id': existing['id'],
                        'n_chunks': existing['n_chunks'], 'added': 0}})
                try:
                    pages = parser.parse_pdf(data)
                    chunks = chunker.chunk_pages(pages)
                except ValueError as e:
                    # parser 消息为已注册的英文源串字面量（见 parser.py 约定）
                    return jsonify({'success': False, 'error': _(str(e))}), 422
                file_id = m.create_fulltext_file(
                    conn, paper_id, f.filename, len(data), sha,
                    len(chunks), _current_user_id())
                m.add_fulltext_chunks(conn, file_id, paper_id, chunks)
                conn.commit()
            return jsonify({'success': True, 'data': {
                'file_id': file_id, 'n_chunks': len(chunks),
                'added': len(chunks)}})
        except Exception:
            return jsonify({'success': False, 'error': _('Internal server error')}), 500

    @bp.route('/api/v1/papers/<paper_id>/fulltext', methods=['GET'])
    def api_list_fulltext(paper_id):
        """分块浏览：?limit=&offset=；附带文件列表。"""
        try:
            limit = min(max(int(request.args.get('limit') or 20), 1), 100)
            offset = max(int(request.args.get('offset') or 0), 0)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': _('Invalid limit or offset')}), 400
        try:
            with m.get_db() as conn:
                files = m.list_fulltext_files(conn, paper_id)
                chunks = m.list_fulltext_chunks(conn, paper_id, limit, offset)
            return jsonify({'success': True,
                            'data': {'files': files, 'chunks': chunks}})
        except Exception:
            return jsonify({'success': False, 'error': _('Internal server error')}), 500

    @bp.route('/api/v1/papers/<paper_id>/fulltext/<file_id>', methods=['DELETE'])
    def api_delete_fulltext(paper_id, file_id):
        """删除指定全文文件；fulltext_chunks 经 ON DELETE CASCADE 级联清理。

        注意：file_id 由蓝图级 before_request validate_uuid_params 前置校验
        （非法 uuid → 400），不会进入 SQL 触发 psycopg2 DataError。
        """
        try:
            with m.get_db() as conn:
                if not m.get_paper(conn, paper_id):
                    return jsonify({'success': False, 'error': _('Paper not found')}), 404
                cur = conn.execute(
                    "DELETE FROM fulltext_files WHERE id = ? AND paper_id = ?",
                    (file_id, paper_id))
                conn.commit()
                return jsonify({'success': True, 'deleted': bool(cur.rowcount)})
        except Exception:
            return jsonify({'success': False, 'error': _('Internal server error')}), 500
