# Aituge Model Configuration

This package is the single source of truth for model metadata and model-pack
composition.

- `components.yaml` registers LLM, Embedding, and Reranker components.
- `packs/*.yaml` composes those components into deployable model packs.
- `ModelRuntimeProvider` is the shared runtime configuration interface used by
  Framework Single Agent and Proof.

Business services pass only a registered `model_id` or use the active model
pack. They do not define provider URLs, model dimensions, context windows, or
default model IDs.

Credential values are resolved by `credential_ref` from the package's
`secrets/` directory. `MODEL_SECRET_DIR` can override that directory and
`MODEL_SECRET_<CREDENTIAL_REF>` can override one credential. During migration,
Framework may read the encrypted credential from `tuge_llm_model`, but it does
not read model metadata from that table.

This makes `aituge_model_config/` a self-contained deployment unit: copy the
directory with its local `secrets/` files and select a pack through
`MODEL_PACK_ID`. Secret files are intentionally ignored by Git and must never be
committed.

## Runtime selection

`MODEL_PACK_ID` selects the deployment default. A Framework Task create request
may additionally provide `model_pack_id`; Framework validates it immediately
and stores the effective value on both Task and Run. The Worker then uses that
frozen value for Single Agent, Pipeline stages, HTTP tools, and result
callbacks. It never changes process-wide environment variables.

Selection precedence is:

1. the Task request's `model_pack_id`;
2. `MODEL_PACK_ID`;
3. `components.yaml`'s `default_pack_id`.

If Java stores a user's preference, Java should place the chosen registered
pack ID on each new Task. Omitting it uses the default. A Run cannot change the
Task's package, so retries are deterministic. An unknown package is rejected
before Task persistence.

`api-rerank` and `local-rerank` deliberately share the same LLM and Embedding
registration. This lets development tests prove package routing through the
stored package ID, runtime provider, and `X-Model-Pack-ID` tool header without
requiring a local inference server; the actual local Reranker connectivity is a
deployment test.
