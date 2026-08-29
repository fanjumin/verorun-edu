-- project_workspace v1.1.0: 适配 text-embedding-004 的 768 维向量。
-- 前置条件：document_chunks.embedding 列内无 1536 维历史数据
-- （Kernel Patch A 之前 UnifiedLLM 无 get_embedding，向量从未成功入库，列基本为空）。
-- 若存在历史 1536 维数据，本迁移将失败并回滚，需先人工清理/重建该列。

ALTER TABLE project_workspace.document_chunks
    ALTER COLUMN embedding TYPE vector(768) USING embedding::vector(768);
