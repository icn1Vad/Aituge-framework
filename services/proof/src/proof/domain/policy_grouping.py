"""Compatibility facade for policy classification imports.

Conflict-oriented classification now lives with the conflict retrieval module,
but existing dataset and benchmark callers keep this stable import path.
"""

from proof.application.conflict_retrieval.taxonomy import (
    LEAF_CATEGORY_NAMES,
    POLICY_CATEGORIES,
    PolicyCategoryDefinition,
    infer_coarse_policy_category,
    infer_policy_category,
)


PolicyGroupDefinition = PolicyCategoryDefinition
POLICY_GROUPS = POLICY_CATEGORIES
POLICY_GROUP_NAMES = LEAF_CATEGORY_NAMES
infer_policy_group = infer_policy_category

__all__ = [
    "POLICY_GROUP_NAMES",
    "POLICY_GROUPS",
    "PolicyGroupDefinition",
    "infer_coarse_policy_category",
    "infer_policy_category",
    "infer_policy_group",
]
