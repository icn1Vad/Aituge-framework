from aituge_model.config import ModelRuntimeProvider


DEFAULT_LLM_MODEL_ID = (
    ModelRuntimeProvider.from_environment().active_pack.llm.id
)
