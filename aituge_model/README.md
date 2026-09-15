# Aituge Model Configuration and Gateway

This package is the single source of truth for model metadata and model-pack
composition, and hosts the internal resilient model gateway.

- `components.yaml` registers LLM, Embedding, Reranker, and streaming speech-recognition components.
- `packs/*.yaml` composes those components into deployable model packs.
- `ModelRuntimeProvider` is the shared runtime configuration interface used by
  Framework Single Agent, Proof, and backend speech recognition.

Business services pass only a registered `model_id` or use the active model
pack. They do not define provider URLs, model dimensions, context windows, or
default model IDs.

Credential values are resolved by `credential_ref` from the package's
`secrets/` directory. `MODEL_SECRET_DIR` can override that directory and
`MODEL_SECRET_<CREDENTIAL_REF>` can override one credential. During migration,
Framework may read the encrypted credential from `tuge_llm_model`, but it does
not read model metadata from that table.

This makes `aituge_model/config/` a self-contained deployment unit: copy the
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

## Speech recognition

`SPEECH_RECOGNITION_MODEL_ID` selects a registered ASR model; otherwise
`default_speech_recognition_id` is used. Credentials use the same `SecretResolver`
as LLM models. The Alibaba NLS registration expects `aliyun_nls_appkey`,
`aliyun_access_key_id`, and `aliyun_access_key_secret`; a short-lived
`aliyun_nls_token` may replace the access-key pair.

Realtime browser audio remains a backend WebSocket service at `/ws/speech`. It
does not pass through the OpenAI-compatible model gateway because that gateway
serves request/response LLM, Embedding, and Reranker protocols rather than a
bidirectional PCM stream.

## Internal gateway

Set `MODEL_GATEWAY_URL` and `MODEL_GATEWAY_TOKEN` in model-consuming services to
route registered LLM, Embedding, and Reranker requests through the internal
gateway. The gateway itself must not set `MODEL_GATEWAY_URL`; it reads the real
upstream URLs and credentials from this package, then exposes OpenAI-compatible
routes on port 18300.

The gateway uses aiohttp for connection pooling and address racing, dnspython
for fallback DNS resolution, Tenacity for bounded retries, and Redis for shared
DNS and circuit-breaker state. It never accepts an arbitrary upstream URL.
