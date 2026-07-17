from __future__ import annotations

import argparse
import json
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import httpx

from proof.config import Settings
from proof.infrastructure.embedding import OpenAICompatibleEmbeddingClient
from proof.infrastructure.postgres.repository import ProofRepository, _vector_text
from proof.tools.benchmark_conflict_retrieval import _load_samples, _load_units, _map_cases


PROMPT_VERSION = "atomic-policy-assertion-focused-v2"
SYSTEM_PROMPT = """你是制度原子规则抽取器。只忠实拆分输入，不做审校、不判断冲突、不补充常识。

输出严格 JSON：
{"items":[{"id":"输入id","assertions":["自包含原子规则1","自包含原子规则2"]}]}

规则：
1. 一条 assertion 只表达一个可以独立判断是否成立或被遵守的要求、禁止、许可、定义、流程关系或关键事实。
2. 可复制同一输入文本中共享的主体、条件和对象，使 assertion 自包含；不得从外部知识补齐部门、阈值、期限或例外。
3. 必须保留原文的数字、单位、否定、强制/许可强度、适用条件、范围、时间、例外和审批终点。
4. 多个并列动作、不同责任主体、不同期限或不同效力必须拆开。
5. 使用简洁自然语言，不输出解释、问题、建议、分类理由或原文引文。
6. 标题、目录或纯背景且不含可比较事实时 assertions 可为空；每个输入最多输出12条。
7. 每个输入 id 恰好返回一次；只输出 JSON。"""
KS = (3, 5, 10, 20)


@dataclass(slots=True)
class ExtractionUsage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, usage: dict[str, Any]) -> None:
        self.calls += 1
        self.input_tokens += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        self.output_tokens += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)


class AtomicExtractor:
    def __init__(self, settings: Settings, model: str) -> None:
        self.endpoint = settings.embedding_base_url.rstrip("/") + "/chat/completions"
        self.api_key = settings.embedding_api_key
        self.model = model
        self.usage = ExtractionUsage()
        self._usage_lock = threading.Lock()

    def extract(self, units: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"items": [{"id": str(unit["id"]), "text": str(unit["text"])} for unit in units]},
                        ensure_ascii=False,
                    ),
                },
            ],
            "temperature": 0,
            "max_tokens": 4000,
            "response_format": {"type": "json_object"},
        }
        last_error = ""
        for attempt in range(3):
            try:
                response = httpx.post(
                    self.endpoint,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=payload,
                    timeout=90,
                )
                response.raise_for_status()
                body = response.json()
                content = str(body["choices"][0]["message"]["content"])
                parsed = _parse_json_object(content)
                result = _validate_extraction_response(parsed, units)
                with self._usage_lock:
                    self.usage.add(body.get("usage") or {})
                missing_ids = {str(unit["id"]) for unit in units} - set(result)
                if missing_ids and len(units) > 1:
                    for unit in units:
                        if str(unit["id"]) in missing_ids:
                            result.update(self.extract([unit]))
                elif missing_ids:
                    raise ValueError(f"Response omitted ids: {sorted(missing_ids)}")
                return result
            except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < 2:
                    time.sleep(0.75 * (2**attempt))
        raise RuntimeError(f"Atomic extraction failed after retries: {last_error}")


def _parse_json_object(content: str) -> dict[str, Any]:
    value = content.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    return json.loads(value)


def _validate_extraction_response(
    parsed: dict[str, Any],
    units: list[dict[str, Any]],
) -> dict[str, list[dict[str, str]]]:
    expected = {str(unit["id"]) for unit in units}
    result: dict[str, list[dict[str, str]]] = {}
    for item in parsed.get("items") or []:
        unit_id = str(item.get("id") or "")
        if unit_id not in expected or unit_id in result:
            continue
        assertions: list[dict[str, str]] = []
        for assertion in item.get("assertions") or []:
            if isinstance(assertion, str):
                text = assertion.strip()
                quote = ""
                kind = "rule"
            else:
                text = str(assertion.get("text") or "").strip()
                quote = str(assertion.get("quote") or "").strip()
                kind = str(assertion.get("kind") or "rule").strip()
            if not text:
                continue
            assertions.append(
                {
                    "text": text,
                    "quote": quote,
                    "kind": kind,
                }
            )
        result[unit_id] = assertions
    return result


def _make_batches(
    units: list[dict[str, Any]],
    *,
    max_units: int = 12,
    max_chars: int = 3000,
) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    chars = 0
    for unit in units:
        size = len(str(unit["text"]))
        if current and (len(current) >= max_units or chars + size > max_chars):
            batches.append(current)
            current = []
            chars = 0
        current.append(unit)
        chars += size
    if current:
        batches.append(current)
    return batches


def _load_or_extract(
    *,
    units: list[dict[str, Any]],
    cache_path: Path,
    extractor: AtomicExtractor,
    concurrency: int,
) -> tuple[dict[str, list[dict[str, str]]], ExtractionUsage]:
    cached: dict[str, list[dict[str, str]]] = {}
    if cache_path.exists():
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        if payload.get("prompt_version") == PROMPT_VERSION and payload.get("model") == extractor.model:
            cached = {str(key): value for key, value in (payload.get("assertions_by_unit") or {}).items()}

    pending = [unit for unit in units if str(unit["id"]) not in cached]
    batches = _make_batches(pending)
    completed = 0
    if batches:
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="atomic-extract") as executor:
            future_map = {executor.submit(extractor.extract, batch): batch for batch in batches}
            for future in as_completed(future_map):
                cached.update(future.result())
                completed += 1
                if completed % 5 == 0 or completed == len(batches):
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(
                        json.dumps(
                            {
                                "prompt_version": PROMPT_VERSION,
                                "model": extractor.model,
                                "assertions_by_unit": cached,
                            },
                            ensure_ascii=False,
                            indent=2,
                        ),
                        encoding="utf-8",
                    )
                print(f"extraction batches: {completed}/{len(batches)}", flush=True)
    return cached, extractor.usage


def _numbers(text: str) -> list[str]:
    return re.findall(r"\d+(?:\.\d+)?%?", text)


def _embed_parallel(
    client: OpenAICompatibleEmbeddingClient,
    texts: list[str],
    *,
    concurrency: int,
) -> list[list[float]]:
    batches = [
        (start, texts[start : start + client.batch_size])
        for start in range(0, len(texts), client.batch_size)
    ]
    vectors_by_start: dict[int, list[list[float]]] = {}
    completed = 0
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="atomic-embed") as executor:
        future_map = {
            executor.submit(client.embed, batch): start
            for start, batch in batches
        }
        for future in as_completed(future_map):
            vectors_by_start[future_map[future]] = future.result()
            completed += 1
            if completed % 25 == 0 or completed == len(batches):
                print(f"embedding batches: {completed}/{len(batches)}", flush=True)
    return [vector for start, _ in batches for vector in vectors_by_start[start]]


def _quality_metrics(
    units: list[dict[str, Any]],
    assertions_by_unit: dict[str, list[dict[str, str]]],
    gold_unit_ids: set[str],
) -> dict[str, Any]:
    unit_by_id = {str(unit["id"]): unit for unit in units}
    assertions = [
        (unit_id, assertion)
        for unit_id, items in assertions_by_unit.items()
        for assertion in items
    ]
    number_errors: list[dict[str, Any]] = []
    omitted_number_errors: list[dict[str, Any]] = []
    for unit_id, assertion in assertions:
        source = str(unit_by_id[unit_id]["text"])
        source_numbers = set(_numbers(source))
        introduced = sorted(set(_numbers(assertion["text"])) - source_numbers)
        if introduced:
            number_errors.append(
                {"unit_id": unit_id, "introduced": introduced, "assertion": assertion["text"]}
            )
    for unit_id, unit in unit_by_id.items():
        source_numbers = set(_numbers(str(unit["text"])))
        assertion_numbers = {
            number
            for assertion in assertions_by_unit.get(unit_id, [])
            for number in _numbers(assertion["text"])
        }
        omitted = sorted(source_numbers - assertion_numbers)
        if omitted:
            omitted_number_errors.append({"unit_id": unit_id, "omitted": omitted})
    empty_gold = sorted(unit_id for unit_id in gold_unit_ids if not assertions_by_unit.get(unit_id))
    total_assertion_chars = sum(len(assertion["text"]) for _, assertion in assertions)
    return {
        "source_units": len(units),
        "units_with_assertions": sum(bool(assertions_by_unit.get(str(unit["id"]))) for unit in units),
        "assertions": len(assertions),
        "assertions_per_source_unit": len(assertions) / len(units),
        "average_assertion_chars": total_assertion_chars / len(assertions) if assertions else 0,
        "quote_exact_rate": None,
        "introduced_number_error_count": len(number_errors),
        "introduced_number_error_examples": number_errors[:20],
        "omitted_number_unit_count": len(omitted_number_errors),
        "omitted_number_unit_examples": omitted_number_errors[:20],
        "empty_gold_unit_ids": empty_gold,
    }


def _create_atomic_temp_table(
    conn: Any,
    *,
    units: list[dict[str, Any]],
    assertions_by_unit: dict[str, list[dict[str, str]]],
    vectors: list[list[float]],
    dimensions: int,
) -> tuple[dict[str, list[dict[str, Any]]], int]:
    unit_by_id = {str(unit["id"]): unit for unit in units}
    atomic_rows: list[tuple[Any, ...]] = []
    atomic_by_unit: dict[str, list[dict[str, Any]]] = defaultdict(list)
    vector_index = 0
    for unit_id, assertions in assertions_by_unit.items():
        unit = unit_by_id[unit_id]
        for assertion_index, assertion in enumerate(assertions):
            atomic_id = f"{unit_id}:{assertion_index}"
            vector = vectors[vector_index]
            vector_index += 1
            atomic_rows.append(
                (
                    atomic_id,
                    unit_id,
                    str(unit["policy_id"]),
                    str(unit["policy_title"]),
                    assertion["text"],
                    _vector_text(vector),
                )
            )
            atomic_by_unit[unit_id].append(
                {"atomic_id": atomic_id, "text": assertion["text"], "vector": vector}
            )
    conn.execute(
        f"""
        CREATE TEMP TABLE atomic_eval_embedding (
          atomic_id text PRIMARY KEY,
          source_unit_id text NOT NULL,
          policy_id text NOT NULL,
          policy_title text NOT NULL,
          text text NOT NULL,
          embedding vector({dimensions}) NOT NULL
        ) ON COMMIT PRESERVE ROWS
        """
    )
    with conn.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO atomic_eval_embedding (
              atomic_id, source_unit_id, policy_id, policy_title, text, embedding
            ) VALUES (%s, %s, %s, %s, %s, %s::vector)
            """,
            atomic_rows,
        )
    conn.commit()
    return atomic_by_unit, len(atomic_rows)


def _raw_ranked(
    conn: Any,
    *,
    query_vector: list[float],
    profile: Any,
    excluded_ids: set[str],
    policy_ids: list[str],
    allowed_unit_ids: list[str],
) -> list[dict[str, Any]]:
    where = [
        "e.profile_id = %s",
        "e.dimensions = %s",
        "u.id = ANY(%s)",
        "NOT (u.id = ANY(%s))",
        "p.status = 'effective'",
    ]
    where_params: list[Any] = [profile.id, profile.dimensions, allowed_unit_ids, sorted(excluded_ids)]
    if policy_ids:
        where.append("u.policy_id = ANY(%s)")
        where_params.append(policy_ids)
    vector = _vector_text(query_vector)
    rows = conn.execute(
        f"""
        SELECT u.id, u.policy_id, u.text, p.title AS policy_title,
               1 - (e.embedding <=> %s::vector) AS score
        FROM proof_retrieval_embedding e
        JOIN proof_retrieval_unit u ON u.id = e.retrieval_unit_id
        JOIN proof_policy p ON p.id = u.policy_id
        WHERE {' AND '.join(where)}
        ORDER BY e.embedding <=> %s::vector
        LIMIT 100
        """,
        [vector, *where_params, vector],
    ).fetchall()
    return [dict(row) for row in rows]


def _atomic_ranked(
    conn: Any,
    *,
    query_vectors: list[list[float]],
    excluded_ids: set[str],
    policy_ids: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_atomic: dict[str, dict[str, Any]] = {}
    where = ["NOT (source_unit_id = ANY(%s))"]
    where_params: list[Any] = [sorted(excluded_ids)]
    if policy_ids:
        where.append("policy_id = ANY(%s)")
        where_params.append(policy_ids)
    for query_vector in query_vectors:
        rows = conn.execute(
            f"""
            SELECT atomic_id, source_unit_id, policy_id, policy_title, text,
                   1 - (embedding <=> %s::vector) AS score
            FROM atomic_eval_embedding
            WHERE {' AND '.join(where)}
            ORDER BY embedding <=> %s::vector
            LIMIT 150
            """,
            [_vector_text(query_vector), *where_params, _vector_text(query_vector)],
        ).fetchall()
        for raw in rows:
            row = dict(raw)
            atomic_id = str(row["atomic_id"])
            if atomic_id not in by_atomic or float(row["score"]) > float(by_atomic[atomic_id]["score"]):
                by_atomic[atomic_id] = row
    assertion_ranked = sorted(by_atomic.values(), key=lambda row: float(row["score"]), reverse=True)
    by_source: dict[str, dict[str, Any]] = {}
    for row in assertion_ranked:
        source_id = str(row["source_unit_id"])
        if source_id not in by_source:
            by_source[source_id] = row
    return assertion_ranked[:100], list(by_source.values())[:100]


def _rank(items: list[dict[str, Any]], target_ids: set[str], *, id_key: str) -> int | None:
    for index, item in enumerate(items, start=1):
        if str(item[id_key]) in target_ids:
            return index
    return None


def _summarize(cases: list[dict[str, Any]], strategy: str) -> dict[str, Any]:
    ranks = [case["strategies"][strategy]["best_rank"] for case in cases]
    group_ranks = [
        rank
        for case in cases
        for rank in case["strategies"][strategy]["target_group_ranks"]
    ]
    return {
        "case_any_target_recall": {
            f"@{k}": sum(rank is not None and rank <= k for rank in ranks) / len(ranks) for k in KS
        },
        "case_all_targets_recall": {
            f"@{k}": sum(
                all(rank is not None and rank <= k for rank in case["strategies"][strategy]["target_group_ranks"])
                for case in cases
            )
            / len(cases)
            for k in KS
        },
        "target_group_recall": {
            f"@{k}": sum(rank is not None and rank <= k for rank in group_ranks) / len(group_ranks)
            for k in KS
        },
        "average_top10_candidate_chars": sum(
            case["strategies"][strategy]["top10_candidate_chars"] for case in cases
        )
        / len(cases),
        "misses_at_10": [
            case["sample_id"]
            for case in cases
            if case["strategies"][strategy]["best_rank"] is None
            or case["strategies"][strategy]["best_rank"] > 10
        ],
    }


def run(
    *,
    output: Path,
    cache: Path,
    chat_model: str,
    concurrency: int,
    candidate_pool_report: Path | None,
) -> dict[str, Any]:
    settings = Settings()
    repository = ProofRepository(settings)
    all_units = _load_units(repository)
    candidate_pool_ids: set[str] | None = None
    if candidate_pool_report is not None:
        retrieval_report = json.loads(candidate_pool_report.read_text(encoding="utf-8"))
        candidate_pool_ids = {
            str(unit_id)
            for case in retrieval_report["cases"]
            for unit_id in (
                list(case["query_unit_ids"])
                + [item for group in case["target_unit_ids"] for item in group]
                + list(case["strategies"]["vector"]["top_ids"])
                + list(case["strategies"]["family_vector"]["top_ids"])
            )
        }
        units = [unit for unit in all_units if str(unit["id"]) in candidate_pool_ids]
    else:
        units = all_units
    registry = Path(__file__).resolve().parents[5] / "数据集/work/tmp/sample_registry.json"
    cases = _map_cases(_load_samples(registry, all_conflicts=True), units)
    gold_unit_ids = {
        str(item["id"])
        for case in cases
        for group in ([case["query_units"]] + case["target_groups"])
        for item in group
    }

    extractor = AtomicExtractor(settings, chat_model)
    assertions_by_unit, usage = _load_or_extract(
        units=units,
        cache_path=cache,
        extractor=extractor,
        concurrency=concurrency,
    )
    query_inputs = [
        {"id": case["sample_id"], "text": case["query_text"]}
        for case in cases
    ]
    query_cache = cache.with_name(f"{cache.stem}-queries{cache.suffix}")
    query_assertions_by_case, usage = _load_or_extract(
        units=query_inputs,
        cache_path=query_cache,
        extractor=extractor,
        concurrency=concurrency,
    )
    quality = _quality_metrics(units, assertions_by_unit, gold_unit_ids)
    query_quality = _quality_metrics(
        query_inputs,
        query_assertions_by_case,
        {case["sample_id"] for case in cases},
    )

    assertion_texts = [
        assertion["text"]
        for unit in units
        for assertion in assertions_by_unit.get(str(unit["id"]), [])
    ]
    embedding_client = OpenAICompatibleEmbeddingClient(settings)
    assertion_vectors = _embed_parallel(
        embedding_client,
        assertion_texts,
        concurrency=concurrency,
    )
    raw_query_vectors = _embed_parallel(
        embedding_client,
        [case["query_text"] for case in cases],
        concurrency=concurrency,
    )
    query_assertion_rows = [
        (case["sample_id"], assertion["text"])
        for case in cases
        for assertion in query_assertions_by_case.get(case["sample_id"], [])
    ]
    query_assertion_vectors = _embed_parallel(
        embedding_client,
        [text for _, text in query_assertion_rows],
        concurrency=concurrency,
    )
    query_vectors_by_case: dict[str, list[list[float]]] = defaultdict(list)
    for (sample_id, _), vector in zip(query_assertion_rows, query_assertion_vectors, strict=True):
        query_vectors_by_case[sample_id].append(vector)

    policy_ids_by_title: dict[str, set[str]] = defaultdict(set)
    for unit in units:
        policy_ids_by_title[str(unit["policy_title"])].add(str(unit["policy_id"]))

    result_cases: list[dict[str, Any]] = []
    allowed_unit_ids = [str(unit["id"]) for unit in units]
    with repository.connect() as conn:
        atomic_by_unit, atomic_count = _create_atomic_temp_table(
            conn,
            units=units,
            assertions_by_unit=assertions_by_unit,
            vectors=assertion_vectors,
            dimensions=embedding_client.dimensions,
        )
        for case, raw_query_vector in zip(cases, raw_query_vectors, strict=True):
            query_ids = {str(item["id"]) for item in case["query_units"]}
            target_groups = [{str(item["id"]) for item in group} for group in case["target_groups"]]
            query_atomic = query_assertions_by_case.get(case["sample_id"], [])
            query_vectors = query_vectors_by_case.get(case["sample_id"], [])
            if not query_vectors:
                query_vectors = [raw_query_vector]
            query_titles = {str(item["policy_title"]) for item in case["query_units"]}
            family_policy_ids = sorted(
                policy_id for title in query_titles for policy_id in policy_ids_by_title[title]
            )

            raw_global = _raw_ranked(
                conn,
                query_vector=raw_query_vector,
                profile=embedding_client.profile,
                excluded_ids=query_ids,
                policy_ids=[],
                allowed_unit_ids=allowed_unit_ids,
            )
            raw_family = _raw_ranked(
                conn,
                query_vector=raw_query_vector,
                profile=embedding_client.profile,
                excluded_ids=query_ids,
                policy_ids=family_policy_ids,
                allowed_unit_ids=allowed_unit_ids,
            )
            atomic_global_assertions, atomic_global_sources = _atomic_ranked(
                conn,
                query_vectors=query_vectors,
                excluded_ids=query_ids,
                policy_ids=[],
            )
            atomic_family_assertions, atomic_family_sources = _atomic_ranked(
                conn,
                query_vectors=query_vectors,
                excluded_ids=query_ids,
                policy_ids=family_policy_ids,
            )
            _, raw_to_atomic_global_sources = _atomic_ranked(
                conn,
                query_vectors=[raw_query_vector],
                excluded_ids=query_ids,
                policy_ids=[],
            )
            _, raw_to_atomic_family_sources = _atomic_ranked(
                conn,
                query_vectors=[raw_query_vector],
                excluded_ids=query_ids,
                policy_ids=family_policy_ids,
            )
            ranked = {
                "raw_global": (raw_global, "id"),
                "raw_family": (raw_family, "id"),
                "atomic_global_source_dedup": (atomic_global_sources, "source_unit_id"),
                "atomic_family_source_dedup": (atomic_family_sources, "source_unit_id"),
                "atomic_global_assertion": (atomic_global_assertions, "source_unit_id"),
                "atomic_family_assertion": (atomic_family_assertions, "source_unit_id"),
                "raw_to_atomic_global_source_dedup": (raw_to_atomic_global_sources, "source_unit_id"),
                "raw_to_atomic_family_source_dedup": (raw_to_atomic_family_sources, "source_unit_id"),
            }
            strategies: dict[str, Any] = {}
            for strategy, (items, id_key) in ranked.items():
                group_ranks = [_rank(items, target_ids, id_key=id_key) for target_ids in target_groups]
                finite = [rank for rank in group_ranks if rank is not None]
                strategies[strategy] = {
                    "best_rank": min(finite) if finite else None,
                    "target_group_ranks": group_ranks,
                    "top10_candidate_chars": sum(len(str(item["text"])) for item in items[:10]),
                }
            result_cases.append(
                {
                    "sample_id": case["sample_id"],
                    "error_type": case["error_type"],
                    "query_assertion_count": len(query_atomic),
                    "target_assertion_counts": [
                        sum(len(atomic_by_unit.get(unit_id, [])) for unit_id in target_ids)
                        for target_ids in target_groups
                    ],
                    "strategies": strategies,
                }
            )

    strategy_names = list(result_cases[0]["strategies"])
    report = {
        "benchmark": {
            "cases": len(result_cases),
            "corpus_source_units": len(units),
            "corpus_scope": "hard_candidate_pool" if candidate_pool_ids is not None else "full_proof_corpus",
            "candidate_pool_report": str(candidate_pool_report) if candidate_pool_report else None,
            "corpus_atomic_assertions": atomic_count,
            "query_direction": "modified_texts[-1] -> modified_texts[:-1]",
            "chat_model": chat_model,
            "embedding_model": embedding_client.model,
            "prompt_version": PROMPT_VERSION,
            "notes": [
                "Raw and atomic variants use the same embedding model and gold source-unit mapping.",
                "Atomic source-dedup ranks source clauses by their best assertion; assertion variants keep duplicate assertions in the candidate budget.",
                "This is a conflict-positive recall benchmark, not an end-to-end accuracy or precision benchmark.",
            ],
        },
        "extraction_usage_this_run": {
            "calls": usage.calls,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
        },
        "extraction_quality": quality,
        "query_extraction_quality": query_quality,
        "summary": {name: _summarize(result_cases, name) for name in strategy_names},
        "cases": result_cases,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and benchmark a temporary Atomic Policy Assertion index.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/conflict-method-eval/atomic-retrieval-benchmark.json"),
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=Path("reports/conflict-method-eval/atomic-assertions-cache.json"),
    )
    parser.add_argument("--chat-model", default="qwen-plus")
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--candidate-pool-report", type=Path)
    args = parser.parse_args()
    report = run(
        output=args.output,
        cache=args.cache,
        chat_model=args.chat_model,
        concurrency=max(1, min(args.concurrency, 12)),
        candidate_pool_report=args.candidate_pool_report,
    )
    print(json.dumps({"quality": report["extraction_quality"], "summary": report["summary"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
