from __future__ import annotations

import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import httpx

from proof.config import Settings
from proof.infrastructure.model_observability import (
    new_logical_call_id,
    observed_tool_attempt,
    usage_token_counts,
)


SYSTEM_PROMPT = """你是制度原子规则抽取结果的完整性检查器。比较 source 与 assertions，只检查是否遗漏，不判断制度是否合理，也不判断规则之间是否矛盾。

每个 item 输出：
- status: complete | incomplete
- missing: assertions 遗漏的原文明确要求、禁止、许可、条件、数字、范围、例外或动作；没有则空数组

特别注意：source 可能故意包含不合理或互相矛盾的测试规则；assertions 忠实拆出这些规则不属于新增，也不能因此判 incomplete。措辞压缩不算遗漏，但改变强度、范围，或漏掉冲突动作的一侧算遗漏。
严格输出 JSON：{"items":[{"id":"...","status":"complete","missing":[]}]}。"""


def _parse(content: str) -> dict[str, Any]:
    value = content.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value)
        value = re.sub(r"\s*```$", "", value)
    return json.loads(value)


def _validate_batch(
    settings: Settings,
    model: str,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    with observed_tool_attempt(
        settings,
        feature_code="proof.offline.atomic_validation",
        model_name=model,
        logical_call_id=new_logical_call_id(),
        attempt_no=1,
        fallback_from_invocation_id=None,
    ) as observation:
        response = httpx.post(
            settings.embedding_base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {settings.embedding_api_key}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"items": items},
                            ensure_ascii=False,
                        ),
                    },
                ],
                "temperature": 0,
                "max_tokens": 3000,
                "response_format": {"type": "json_object"},
            },
            timeout=90,
        )
        observation.dispatched(response)
        response.raise_for_status()
        body = response.json()
        parsed = _parse(str(body["choices"][0]["message"]["content"]))
        expected = {item["id"] for item in items}
        results = [
            item
            for item in parsed.get("items") or []
            if item.get("id") in expected
        ]
        if {item.get("id") for item in results} != expected:
            raise RuntimeError("Validator omitted one or more ids")
        input_tokens, output_tokens = usage_token_counts(
            body.get("usage") or {}
        )
        observation.succeeded(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate exact-query atomic extraction fidelity.")
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="qwen-plus")
    args = parser.parse_args()

    settings = Settings()
    cache = json.loads(args.cache.read_text(encoding="utf-8"))["assertions_by_unit"]
    registry = json.loads(args.registry.read_text(encoding="utf-8"))
    sources = {
        item["sample_id"]: item["modified_texts"][-1]
        for item in registry
        if item.get("taxonomy_category") == "冲突类"
    }
    items = [
        {
            "id": sample_id,
            "source": source,
            "assertions": [assertion["text"] for assertion in cache[sample_id]],
        }
        for sample_id, source in sources.items()
    ]
    batches = [items[start : start + 10] for start in range(0, len(items), 10)]
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=7, thread_name_prefix="atomic-validate") as executor:
        futures = [executor.submit(_validate_batch, settings, args.model, batch) for batch in batches]
        for future in as_completed(futures):
            results.extend(future.result())
    results.sort(key=lambda item: item["id"])
    counts = {
        status: sum(item.get("status") == status for item in results)
        for status in ("complete", "incomplete")
    }
    report = {
        "model": args.model,
        "items": len(results),
        "counts": counts,
        "complete_rate": counts["complete"] / len(results),
        "results": results,
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "results"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
