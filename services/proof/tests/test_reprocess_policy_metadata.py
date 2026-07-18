from proof.tools.reprocess_policy_metadata import reprocess_policy_metadata


class MemoryMetadataRepository:
    def __init__(self) -> None:
        self.rows = [
            {
                "id": "p1",
                "title": "《采购管理办法（试行）》",
                "normalized_title": "",
                "category_code": "general",
            },
            {
                "id": "p2",
                "title": "自定义专项制度",
                "normalized_title": "自定义专项制度",
                "category_code": "other",
            },
        ]

    def list_policy_metadata(self):
        return [dict(row) for row in self.rows]

    def update_policy_metadata(self, policy_id, *, normalized_title, category_code=None):
        row = next(item for item in self.rows if item["id"] == policy_id)
        changed = row["normalized_title"] != normalized_title
        row["normalized_title"] = normalized_title
        if category_code is not None:
            changed = changed or row["category_code"] != category_code
            row["category_code"] = category_code
        return changed


def test_policy_metadata_reprocessing_is_idempotent() -> None:
    repository = MemoryMetadataRepository()

    first = reprocess_policy_metadata(repository)
    second = reprocess_policy_metadata(repository)

    assert first == {
        "dry_run": False,
        "scanned_count": 2,
        "changed_count": 1,
        "normalized_title_update_count": 1,
        "category_update_count": 1,
    }
    assert second["changed_count"] == 0
    assert repository.rows[0]["normalized_title"] == "采购管理"
    assert repository.rows[0]["category_code"] == "procurement_supply"
