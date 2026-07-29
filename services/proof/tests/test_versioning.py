from proof.versioning import format_policy_version


def test_policy_version_display_carries_every_ten_patch_versions() -> None:
    assert format_policy_version(0) == "v1.0.0"
    assert format_policy_version(9) == "v1.0.9"
    assert format_policy_version(10) == "v1.1.0"
    assert format_policy_version(99) == "v1.9.9"
    assert format_policy_version(100) == "v2.0.0"
