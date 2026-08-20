from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row

from qianxuesen_mentor.catalog import CATALOG
from qianxuesen_mentor.config import Settings
from qianxuesen_mentor.errors import QianXuesenError


class Repository:
    def __init__(self, settings: Settings) -> None:
        if not settings.database_url:
            raise QianXuesenError("database_unconfigured", "QXS_DATABASE_URL is required", status_code=503)
        self.settings = settings

    def _connect(self):
        return psycopg.connect(self.settings.database_url, row_factory=dict_row)

    def health(self) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("""
                SELECT count(*)::integer AS documents,
                       coalesce(sum(page_count), 0)::integer AS pages,
                       (SELECT count(*)::integer FROM qxs_book_chunk) AS chunks
                FROM qxs_document d
            """).fetchone()
        return dict(row or {})

    def upsert_catalog(self) -> int:
        root = self.settings.resolved_corpus_root()
        with self._connect() as conn:
            for entry in CATALOG:
                path = root / entry.filename
                conn.execute("""
                    INSERT INTO qxs_document(
                      id,title,original_name,author,publication_year,category,source_kind,
                      page_count,storage_path,file_size,document_status,updated_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                    ON CONFLICT (id) DO UPDATE SET
                      title=excluded.title, author=excluded.author,
                      publication_year=excluded.publication_year, category=excluded.category,
                      source_kind=excluded.source_kind, page_count=excluded.page_count,
                      storage_path=excluded.storage_path, file_size=excluded.file_size,
                      document_status=CASE
                        WHEN excluded.file_size IS NULL THEN 'missing'
                        WHEN qxs_document.document_status='missing' THEN 'cataloged'
                        ELSE qxs_document.document_status
                      END,
                      updated_at=now()
                """, (
                    entry.id, entry.title, entry.filename, entry.author, entry.publication_year,
                    entry.category, entry.source_kind, entry.page_count, str(path),
                    path.stat().st_size if path.is_file() else None,
                    "cataloged" if path.is_file() else "missing",
                ))
            conn.commit()
        return len(CATALOG)

    def list_files(self) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT d.*, count(c.id)::integer AS chunk_count
                FROM qxs_document d LEFT JOIN qxs_book_chunk c ON c.document_id=d.id
                GROUP BY d.id ORDER BY d.category, d.title
            """).fetchall()
        return {"items": [self._file_view(row) for row in rows], "total": len(rows)}

    def get_document(self, document_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM qxs_document WHERE id=%s", (document_id,)).fetchone()
        if not row:
            raise QianXuesenError("document_not_found", "资料不存在", status_code=404)
        return dict(row)

    def list_chunks(self, document_id: str, *, limit: int, offset: int) -> dict[str, Any]:
        self.get_document(document_id)
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT * FROM qxs_book_chunk WHERE document_id=%s ORDER BY ordinal LIMIT %s OFFSET %s
            """, (document_id, limit, offset)).fetchall()
            total = conn.execute("SELECT count(*)::integer AS n FROM qxs_book_chunk WHERE document_id=%s", (document_id,)).fetchone()["n"]
        items = [{
            "id": row["id"], "clause_no_raw": row["chapter"] or f"第{row['ordinal']}片段",
            "clause_ordinal": row["ordinal"], "unit_type": "book_chunk",
            "content": row["content"], "heading_path": row["heading_path"],
            "page_start": row["page_start"], "page_end": row["page_end"],
            "paragraph_start": None, "paragraph_end": None, "char_start": None, "char_end": None,
            "text_hash": row["text_hash"], "embedding_status": row["embedding_status"],
        } for row in rows]
        return {"file_id": document_id, "items": items, "limit": limit, "offset": offset,
                "total": total, "has_more": offset + len(items) < total}

    def page_statuses(self, document_id: str) -> dict[int, str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT page_no, processing_status FROM qxs_page WHERE document_id=%s", (document_id,)).fetchall()
        return {int(row["page_no"]): str(row["processing_status"]) for row in rows}

    def upsert_page(self, document_id: str, page_no: int, *, native_text: str, ocr_text: str,
                    method: str, status: str, quality_score: float, layout: dict | None = None,
                    error_code: str | None = None) -> None:
        effective = ocr_text if method == "ocr" else native_text
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO qxs_page(document_id,page_no,native_text,ocr_text,effective_text,
                  extraction_method,processing_status,quality_score,layout_json,error_code,updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,now())
                ON CONFLICT(document_id,page_no) DO UPDATE SET
                  native_text=excluded.native_text,ocr_text=excluded.ocr_text,effective_text=excluded.effective_text,
                  extraction_method=excluded.extraction_method,processing_status=excluded.processing_status,
                  quality_score=excluded.quality_score,layout_json=excluded.layout_json,error_code=excluded.error_code,
                  updated_at=now()
            """, (document_id, page_no, native_text, ocr_text, effective, method, status,
                    quality_score, json.dumps(layout or {}, ensure_ascii=False), error_code))
            conn.commit()

    def document_pages(self, document_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("""
                SELECT page_no,effective_text,quality_score,processing_status FROM qxs_page
                WHERE document_id=%s AND effective_text<>'' ORDER BY page_no
            """, (document_id,)).fetchall()]

    def replace_chunks(self, document_id: str, chunks: Iterable[dict[str, Any]]) -> int:
        chunks = list(chunks)
        with self._connect() as conn:
            conn.execute("DELETE FROM qxs_book_chunk WHERE document_id=%s", (document_id,))
            for chunk in chunks:
                conn.execute("""
                    INSERT INTO qxs_book_chunk(id,document_id,ordinal,chapter,heading_path,page_start,page_end,
                      content,parent_content,text_hash,embedding_status)
                    VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,'pending')
                """, (chunk["id"], document_id, chunk["ordinal"], chunk["chapter"],
                      json.dumps(chunk["heading_path"], ensure_ascii=False), chunk["page_start"], chunk["page_end"],
                      chunk["content"], chunk["parent_content"], chunk["text_hash"]))
            conn.execute("UPDATE qxs_document SET index_status=%s,document_status='parsed',updated_at=now() WHERE id=%s",
                         ("keyword_indexed" if chunks else "empty", document_id))
            conn.commit()
        return len(chunks)

    def chunks_for_cards(self, document_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("""
                SELECT c.*,d.title AS document_title,d.source_kind FROM qxs_book_chunk c
                JOIN qxs_document d ON d.id=c.document_id WHERE c.document_id=%s ORDER BY c.ordinal
            """, (document_id,)).fetchall()]

    def replace_cards(self, document_id: str, facts: list[dict[str, Any]], principles: list[dict[str, Any]]) -> tuple[int, int]:
        with self._connect() as conn:
            conn.execute("""
                DELETE FROM qxs_card_embedding e USING qxs_fact f
                WHERE e.evidence_type='fact' AND e.card_id=f.id AND f.document_id=%s
            """, (document_id,))
            conn.execute("""
                DELETE FROM qxs_card_embedding e USING qxs_principle p
                WHERE e.evidence_type='principle' AND e.card_id=p.id AND p.document_id=%s
            """, (document_id,))
            conn.execute("DELETE FROM qxs_fact WHERE document_id=%s", (document_id,))
            conn.execute("DELETE FROM qxs_principle WHERE document_id=%s", (document_id,))
            for item in facts:
                conn.execute("""
                    INSERT INTO qxs_fact(id,predicate,value,event_date,event_year,document_id,chunk_id,
                      page_start,page_end,source_count,confidence,card_status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (item["id"], item["predicate"], item["value"], item.get("event_date"), item.get("event_year"),
                      document_id, item["chunk_id"], item["page_start"], item["page_end"], item["source_count"],
                      item["confidence"], item["card_status"]))
            for item in principles:
                conn.execute("""
                    INSERT INTO qxs_principle(id,title,summary,application,constraints,document_id,chunk_id,
                      page_start,page_end,confidence,card_status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (item["id"], item["title"], item["summary"], item["application"], item["constraints"],
                      document_id, item["chunk_id"], item["page_start"], item["page_end"],
                      item["confidence"], item["card_status"]))
            conn.commit()
        return len(facts), len(principles)

    def index_chunk_embeddings(self, items: list[tuple[str, list[float]]], *, profile_id: str, model: str) -> None:
        if not items:
            return
        with self._connect() as conn:
            for chunk_id, embedding in items:
                conn.execute("""
                    INSERT INTO qxs_book_embedding(chunk_id,profile_id,model,dimensions,embedding)
                    VALUES (%s,%s,%s,%s,%s::vector)
                    ON CONFLICT(chunk_id) DO UPDATE SET profile_id=excluded.profile_id,model=excluded.model,
                      dimensions=excluded.dimensions,embedding=excluded.embedding,updated_at=now()
                """, (chunk_id, profile_id, model, len(embedding), json.dumps(embedding)))
            conn.execute(
                "UPDATE qxs_book_chunk SET embedding_status='indexed',updated_at=now() WHERE id=ANY(%s)",
                ([chunk_id for chunk_id, _ in items],),
            )
            conn.commit()

    def card_texts(self, document_id: str) -> list[dict[str, str]]:
        with self._connect() as conn:
            facts = conn.execute("SELECT id,predicate || ' ' || value AS text FROM qxs_fact WHERE document_id=%s", (document_id,)).fetchall()
            principles = conn.execute("SELECT id,title || ' ' || summary || ' ' || application AS text FROM qxs_principle WHERE document_id=%s", (document_id,)).fetchall()
        return ([{"evidence_type": "fact", **dict(row)} for row in facts]
                + [{"evidence_type": "principle", **dict(row)} for row in principles])

    def index_card_embeddings(self, items: list[tuple[str, str, list[float]]], *, profile_id: str, model: str) -> None:
        if not items:
            return
        with self._connect() as conn:
            for evidence_type, card_id, embedding in items:
                conn.execute("""
                    INSERT INTO qxs_card_embedding(evidence_type,card_id,profile_id,model,dimensions,embedding)
                    VALUES (%s,%s,%s,%s,%s,%s::vector)
                    ON CONFLICT(evidence_type,card_id) DO UPDATE SET profile_id=excluded.profile_id,
                      model=excluded.model,dimensions=excluded.dimensions,embedding=excluded.embedding,updated_at=now()
                """, (evidence_type, card_id, profile_id, model, len(embedding), json.dumps(embedding)))
            conn.commit()

    def pending_chunk_embeddings(self, *, profile_id: str, model: str, dimensions: int,
                                 limit: int, document_ids: set[str] | None = None) -> list[dict[str, str]]:
        document_clause, document_params = self._document_filter("c", document_ids)
        with self._connect() as conn:
            rows = conn.execute(f"""
                SELECT c.id,c.content FROM qxs_book_chunk c
                LEFT JOIN qxs_book_embedding e ON e.chunk_id=c.id
                WHERE (e.chunk_id IS NULL OR e.profile_id<>%s OR e.model<>%s OR e.dimensions<>%s)
                {document_clause}
                ORDER BY c.document_id,c.ordinal LIMIT %s
            """, (profile_id, model, dimensions, *document_params, limit)).fetchall()
        return [dict(row) for row in rows]

    def pending_card_embeddings(self, *, profile_id: str, model: str, dimensions: int,
                                limit: int, document_ids: set[str] | None = None) -> list[dict[str, str]]:
        fact_clause, fact_params = self._document_filter("f", document_ids)
        principle_clause, principle_params = self._document_filter("p", document_ids)
        with self._connect() as conn:
            rows = conn.execute(f"""
                SELECT 'fact'::text AS evidence_type,f.id,f.predicate || ' ' || f.value AS text,
                       f.document_id,0 AS type_order
                FROM qxs_fact f LEFT JOIN qxs_card_embedding e
                  ON e.evidence_type='fact' AND e.card_id=f.id
                WHERE (e.card_id IS NULL OR e.profile_id<>%s OR e.model<>%s OR e.dimensions<>%s)
                {fact_clause}
                UNION ALL
                SELECT 'principle'::text AS evidence_type,p.id,
                       p.title || ' ' || p.summary || ' ' || p.application AS text,
                       p.document_id,1 AS type_order
                FROM qxs_principle p LEFT JOIN qxs_card_embedding e
                  ON e.evidence_type='principle' AND e.card_id=p.id
                WHERE (e.card_id IS NULL OR e.profile_id<>%s OR e.model<>%s OR e.dimensions<>%s)
                {principle_clause}
                ORDER BY document_id,type_order,id LIMIT %s
            """, (
                profile_id, model, dimensions, *fact_params,
                profile_id, model, dimensions, *principle_params, limit,
            )).fetchall()
        return [dict(row) for row in rows]

    def embedding_progress(self, *, profile_id: str, model: str, dimensions: int,
                           document_ids: set[str] | None = None) -> dict[str, int]:
        chunk_clause, chunk_params = self._document_filter("c", document_ids)
        fact_clause, fact_params = self._document_filter("f", document_ids)
        principle_clause, principle_params = self._document_filter("p", document_ids)
        with self._connect() as conn:
            chunk = conn.execute(f"""
                SELECT count(*)::integer AS total,
                  count(e.chunk_id) FILTER (WHERE e.profile_id=%s AND e.model=%s AND e.dimensions=%s)::integer AS indexed
                FROM qxs_book_chunk c LEFT JOIN qxs_book_embedding e ON e.chunk_id=c.id
                WHERE true {chunk_clause}
            """, (profile_id, model, dimensions, *chunk_params)).fetchone()
            cards = conn.execute(f"""
                SELECT sum(total)::integer AS total,sum(indexed)::integer AS indexed FROM (
                  SELECT count(*) AS total,
                    count(e.card_id) FILTER (WHERE e.profile_id=%s AND e.model=%s AND e.dimensions=%s) AS indexed
                  FROM qxs_fact f LEFT JOIN qxs_card_embedding e
                    ON e.evidence_type='fact' AND e.card_id=f.id WHERE true {fact_clause}
                  UNION ALL
                  SELECT count(*) AS total,
                    count(e.card_id) FILTER (WHERE e.profile_id=%s AND e.model=%s AND e.dimensions=%s) AS indexed
                  FROM qxs_principle p LEFT JOIN qxs_card_embedding e
                    ON e.evidence_type='principle' AND e.card_id=p.id WHERE true {principle_clause}
                ) counts
            """, (
                profile_id, model, dimensions, *fact_params,
                profile_id, model, dimensions, *principle_params,
            )).fetchone()
        return {
            "chunks_total": int(chunk["total"] or 0), "chunks_indexed": int(chunk["indexed"] or 0),
            "cards_total": int(cards["total"] or 0), "cards_indexed": int(cards["indexed"] or 0),
        }

    def refresh_document_index_status(self, *, profile_id: str, model: str, dimensions: int,
                                      document_ids: set[str] | None = None) -> None:
        document_clause = "AND d.id=ANY(%s)" if document_ids else ""
        document_params = (sorted(document_ids),) if document_ids else ()
        with self._connect() as conn:
            conn.execute(f"""
                UPDATE qxs_document d SET index_status=CASE WHEN
                  NOT EXISTS (
                    SELECT 1 FROM qxs_book_chunk c LEFT JOIN qxs_book_embedding e ON e.chunk_id=c.id
                    WHERE c.document_id=d.id
                      AND (e.chunk_id IS NULL OR e.profile_id<>%s OR e.model<>%s OR e.dimensions<>%s)
                  ) AND NOT EXISTS (
                    SELECT 1 FROM qxs_fact f LEFT JOIN qxs_card_embedding e
                      ON e.evidence_type='fact' AND e.card_id=f.id
                    WHERE f.document_id=d.id
                      AND (e.card_id IS NULL OR e.profile_id<>%s OR e.model<>%s OR e.dimensions<>%s)
                  ) AND NOT EXISTS (
                    SELECT 1 FROM qxs_principle p LEFT JOIN qxs_card_embedding e
                      ON e.evidence_type='principle' AND e.card_id=p.id
                    WHERE p.document_id=d.id
                      AND (e.card_id IS NULL OR e.profile_id<>%s OR e.model<>%s OR e.dimensions<>%s)
                  ) THEN 'indexed' ELSE 'keyword_indexed' END,updated_at=now()
                WHERE true {document_clause}
            """, (
                profile_id, model, dimensions, profile_id, model, dimensions,
                profile_id, model, dimensions, *document_params,
            ))
            conn.commit()

    @staticmethod
    def _document_filter(alias: str, document_ids: set[str] | None) -> tuple[str, tuple[Any, ...]]:
        if not document_ids:
            return "", ()
        return f"AND {alias}.document_id=ANY(%s)", (sorted(document_ids),)

    def create_run(self, run_id: str, document_id: str, total_pages: int) -> None:
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO qxs_ingestion_run(id,document_id,status,total_pages)
                VALUES (%s,%s,'running',%s)
                ON CONFLICT(id) DO UPDATE SET status='running',error_summary=NULL,updated_at=now()
            """, (run_id, document_id, total_pages))
            conn.commit()

    def update_run(self, run_id: str, *, current_page: int, processed_pages: int,
                   ocr_pages: int, failed_pages: int, status: str = "running",
                   error_summary: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute("""
                UPDATE qxs_ingestion_run SET current_page=%s,processed_pages=%s,ocr_pages=%s,
                  failed_pages=%s,status=%s,error_summary=%s,updated_at=now(),
                  completed_at=CASE WHEN %s IN ('completed','failed','interrupted') THEN now() ELSE completed_at END
                WHERE id=%s
            """, (current_page, processed_pages, ocr_pages, failed_pages, status,
                  error_summary, status, run_id))
            conn.commit()

    def finish_document(self, document_id: str, *, sha256: str, ocr_status: str, index_status: str) -> None:
        with self._connect() as conn:
            conn.execute("""
                UPDATE qxs_document SET content_sha256=%s,document_status='parsed',ocr_status=%s,
                  index_status=%s,updated_at=now() WHERE id=%s
            """, (sha256, ocr_status, index_status, document_id))
            conn.commit()

    def keyword_search(self, layer: str, query: str, limit: int) -> list[dict[str, Any]]:
        sql = {
            "fact": """SELECT f.id,f.value AS content,f.predicate AS title,f.document_id,f.chunk_id,
                       f.page_start,f.page_end,f.confidence,f.card_status,d.title AS document_name,
                       ts_rank_cd(f.search_vector,plainto_tsquery('jiebacfg',%s)) AS score
                       FROM qxs_fact f JOIN qxs_document d ON d.id=f.document_id
                       WHERE f.search_vector @@ plainto_tsquery('jiebacfg',%s) AND f.card_status='auto_published'
                       ORDER BY score DESC LIMIT %s""",
            "principle": """SELECT p.id,p.summary AS content,p.title,p.document_id,p.chunk_id,
                       p.page_start,p.page_end,p.confidence,p.card_status,d.title AS document_name,
                       ts_rank_cd(p.search_vector,plainto_tsquery('jiebacfg',%s)) AS score
                       FROM qxs_principle p JOIN qxs_document d ON d.id=p.document_id
                       WHERE p.search_vector @@ plainto_tsquery('jiebacfg',%s) AND p.card_status='auto_published'
                       ORDER BY score DESC LIMIT %s""",
            "book": """SELECT c.id,c.content,c.chapter AS title,c.document_id,c.id AS chunk_id,
                       c.page_start,c.page_end,1.0::double precision AS confidence,'source' AS card_status,
                       d.title AS document_name,
                       ts_rank_cd(c.search_vector,plainto_tsquery('jiebacfg',%s)) AS score
                       FROM qxs_book_chunk c JOIN qxs_document d ON d.id=c.document_id
                       WHERE c.search_vector @@ plainto_tsquery('jiebacfg',%s)
                       ORDER BY score DESC LIMIT %s""",
        }[layer]
        with self._connect() as conn:
            rows = conn.execute(sql, (query, query, limit)).fetchall()
        return [dict(row, evidence_type=layer) for row in rows]

    def vector_search(self, layer: str, embedding: list[float], profile_id: str, limit: int) -> list[dict[str, Any]]:
        vector = json.dumps(embedding)
        if layer == "book":
            sql = """SELECT c.id,c.content,c.chapter AS title,c.document_id,c.id AS chunk_id,c.page_start,c.page_end,
                     1.0::double precision AS confidence,'source' AS card_status,d.title AS document_name,
                     1-(e.embedding <=> %s::vector) AS score FROM qxs_book_embedding e
                     JOIN qxs_book_chunk c ON c.id=e.chunk_id JOIN qxs_document d ON d.id=c.document_id
                     WHERE e.profile_id=%s ORDER BY e.embedding <=> %s::vector LIMIT %s"""
            params = (vector, profile_id, vector, limit)
        else:
            table = "qxs_fact" if layer == "fact" else "qxs_principle"
            content = "f.value" if layer == "fact" else "f.summary"
            title = "f.predicate" if layer == "fact" else "f.title"
            sql = f"""SELECT f.id,{content} AS content,{title} AS title,f.document_id,f.chunk_id,f.page_start,f.page_end,
                     f.confidence,f.card_status,d.title AS document_name,1-(e.embedding <=> %s::vector) AS score
                     FROM qxs_card_embedding e JOIN {table} f ON f.id=e.card_id
                     JOIN qxs_document d ON d.id=f.document_id
                     WHERE e.evidence_type=%s AND e.profile_id=%s AND f.card_status='auto_published'
                     ORDER BY e.embedding <=> %s::vector LIMIT %s"""
            params = (vector, layer, profile_id, vector, limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row, evidence_type=layer) for row in rows]

    def execute_read_query(self, sql: str, *, timeout_ms: int, row_limit: int) -> dict[str, Any]:
        started = time.perf_counter()
        with self._connect() as conn:
            conn.execute("BEGIN READ ONLY")
            conn.execute("SELECT set_config('statement_timeout', %s, true)", (f"{timeout_ms}ms",))
            cursor = conn.execute(sql)
            rows = cursor.fetchmany(row_limit + 1)
            columns = [column.name for column in cursor.description or []]
            conn.rollback()
        return {"columns": columns, "rows": [dict(row) for row in rows],
                "execution_ms": round((time.perf_counter() - started) * 1000, 2)}

    def style_samples(self, limit: int = 40) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT c.id AS chunk_id,d.title AS document_name,d.source_kind,c.page_start,c.page_end,c.content
                FROM qxs_book_chunk c JOIN qxs_document d ON d.id=c.document_id
                WHERE d.source_kind IN ('authored','letters') AND length(c.content) BETWEEN 120 AND 1000
                ORDER BY d.source_kind,d.title,c.ordinal LIMIT %s
            """, (limit,)).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _file_view(row: dict[str, Any]) -> dict[str, Any]:
        year = str(row["publication_year"]) if row.get("publication_year") else None
        return {
            "id": row["id"], "name": row["original_name"], "file_type": "pdf",
            "document_status": row["document_status"], "created_at": row["created_at"].isoformat(),
            "updated_at": row["updated_at"].isoformat(), "title": row["title"], "author": row["author"],
            "publication_year": row["publication_year"], "category": row["category"],
            "page_count": row["page_count"], "ocr_status": row["ocr_status"],
            "index_status": row["index_status"], "chunk_count": row["chunk_count"],
            # Backward-compatible aliases during the Java/front-end staged cutover.
            "policy_id": row["id"], "policy_title": row["title"], "policy_version": year,
            "policy_status": row["index_status"], "pending_action": None, "operation_status": None,
            "blocking_reader_count": 0, "operation_id": None, "framework_task_id": None, "framework_run_id": None,
        }
