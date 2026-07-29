from aituge_model_config import ModelRuntimeProvider


DEFAULT_LLM_MODEL_ID = (
    ModelRuntimeProvider.from_environment().active_pack.llm.id
)
