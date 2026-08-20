CREATE OR REPLACE VIEW qxs_sql_document_v AS
SELECT d.id AS document_id, d.title, d.author, d.publication_year, d.category,
       d.source_kind, d.page_count, d.document_status, d.ocr_status, d.index_status,
       count(DISTINCT p.page_no)::integer AS processed_page_count,
       count(DISTINCT c.id)::integer AS chunk_count, d.updated_at
FROM qxs_document d
LEFT JOIN qxs_page p ON p.document_id = d.id
  AND p.processing_status IN ('extracted', 'ocr_success', 'blank', 'needs_ocr')
LEFT JOIN qxs_book_chunk c ON c.document_id = d.id
GROUP BY d.id;

CREATE OR REPLACE VIEW qxs_document_processing_v AS
SELECT d.id,
       d.title,
       d.page_count,
       count(p.page_no) AS recorded_pages,
       count(p.page_no) FILTER (
         WHERE p.processing_status IN ('extracted', 'ocr_success', 'blank', 'needs_ocr')
       ) AS processed_pages,
       count(p.page_no) FILTER (WHERE p.processing_status = 'ocr_success') AS ocr_pages,
       count(p.page_no) FILTER (WHERE p.processing_status = 'blank') AS blank_pages,
       count(p.page_no) FILTER (WHERE p.processing_status = 'needs_ocr') AS needs_ocr_pages,
       count(p.page_no) FILTER (WHERE p.processing_status = 'failed') AS failed_pages
FROM qxs_document d
LEFT JOIN qxs_page p ON p.document_id = d.id
GROUP BY d.id, d.title, d.page_count;
