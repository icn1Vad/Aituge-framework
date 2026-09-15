--
-- AI-TUGE framework schema baseline. PostgreSQL only.
--

-- Dumped from database version 16.14 (Debian 16.14-1.pgdg12+1)
-- Dumped by pg_dump version 16.14 (Debian 16.14-1.pgdg12+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: tuge_agent_profile; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tuge_agent_profile (
    agent_id character varying(80) NOT NULL,
    name character varying(120) NOT NULL,
    description character varying NOT NULL,
    agent_type character varying(32) NOT NULL,
    model_id character varying(120) NOT NULL,
    system_prompt character varying NOT NULL,
    default_tools_json character varying NOT NULL,
    default_datasets_json character varying NOT NULL,
    runtime_config_json character varying NOT NULL,
    enabled boolean NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL
);


--
-- Name: tuge_attachment; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tuge_attachment (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    purpose character varying(32) NOT NULL,
    alias_id character varying(64),
    file_name character varying(255) NOT NULL,
    file_extension character varying(32),
    file_size integer NOT NULL,
    file_md5 character varying(64),
    file_path text,
    mime_type character varying(128),
    status character varying(32) NOT NULL,
    failed_reason text,
    ref_count integer NOT NULL,
    expires_at timestamp without time zone,
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    file_metadata json
);


--
-- Name: tuge_attachment_chunk; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tuge_attachment_chunk (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    file_id character varying(64) NOT NULL,
    chunk_index integer NOT NULL,
    content text,
    start_offset integer NOT NULL,
    end_offset integer NOT NULL,
    token_count integer NOT NULL,
    chunk_metadata json,
    created_at timestamp without time zone
);


--
-- Name: tuge_attachment_reference_owner; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tuge_attachment_reference_owner (
    tenant_id character varying(64) NOT NULL,
    owner_id character varying(128) NOT NULL,
    revision integer NOT NULL,
    file_ids json
);


--
-- Name: tuge_attachment_text_content; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tuge_attachment_text_content (
    file_id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    content text,
    content_length integer NOT NULL,
    extractor_version character varying(32),
    created_at timestamp without time zone,
    updated_at timestamp without time zone
);


--
-- Name: tuge_attachment_upload_session; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tuge_attachment_upload_session (
    id character varying(64) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    file_name character varying(255) NOT NULL,
    purpose character varying(32) NOT NULL,
    expires_in_seconds integer,
    status character varying(32) NOT NULL,
    expires_at timestamp without time zone,
    parts json,
    file_id character varying(64),
    created_at timestamp without time zone,
    updated_at timestamp without time zone
);


--
-- Name: tuge_discussion_participant; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tuge_discussion_participant (
    id character varying NOT NULL,
    run_id character varying(80) NOT NULL,
    agent_id character varying(80) NOT NULL,
    agent_session_id character varying(160) NOT NULL,
    agent_thread_id character varying(80) NOT NULL,
    agent_profile_snapshot json,
    "position" integer NOT NULL,
    enabled boolean NOT NULL,
    created_at timestamp without time zone
);


--
-- Name: tuge_discussion_run; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tuge_discussion_run (
    id character varying NOT NULL,
    public_thread_id character varying(80) NOT NULL,
    user_id character varying(120) NOT NULL,
    topic text,
    participant_agent_ids json,
    moderator_agent_id character varying(80),
    status character varying(32) NOT NULL,
    max_rounds integer NOT NULL,
    current_round integer NOT NULL,
    final_output json,
    created_at timestamp without time zone,
    updated_at timestamp without time zone,
    started_at timestamp without time zone,
    finished_at timestamp without time zone
);


--
-- Name: tuge_discussion_turn; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tuge_discussion_turn (
    id character varying NOT NULL,
    run_id character varying(80) NOT NULL,
    round_index integer NOT NULL,
    turn_index integer NOT NULL,
    agent_id character varying(80) NOT NULL,
    action character varying(32),
    reason text,
    public_message_id character varying(80),
    private_thread_id character varying(80),
    private_session_id character varying(160),
    status character varying(32) NOT NULL,
    response_json json,
    tool_steps_json json,
    skills_json json,
    error text,
    created_at timestamp without time zone,
    started_at timestamp without time zone,
    finished_at timestamp without time zone
);


--
-- Name: tuge_llm_model; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tuge_llm_model (
    tenant_id character varying(64),
    base_url character varying NOT NULL,
    model character varying NOT NULL,
    model_name character varying,
    context_window integer NOT NULL,
    temperature double precision NOT NULL,
    model_id character varying(64) NOT NULL,
    enabled boolean NOT NULL,
    vision_support boolean NOT NULL,
    max_tokens integer NOT NULL,
    enable_thinking boolean NOT NULL,
    provider_name character varying,
    source character varying NOT NULL,
    id character varying(64) NOT NULL,
    encrypted_api_key character varying
);


--
-- Name: tuge_message; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tuge_message (
    id character varying NOT NULL,
    thread_id character varying NOT NULL,
    local_id character varying,
    tenant_id character varying,
    role character varying NOT NULL,
    content json,
    attachments json,
    token_usage json,
    created_at timestamp without time zone
);


--
-- Name: tuge_obs30_query_snapshot; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tuge_obs30_query_snapshot (
    handle character varying(84) NOT NULL,
    query_snapshot_id character varying(80) NOT NULL,
    resource_kind character varying(48) NOT NULL,
    snapshot_mode character varying(40) NOT NULL,
    scope_hash character varying(64) NOT NULL,
    filter_hash character varying(64) NOT NULL,
    snapshot_to timestamp without time zone NOT NULL,
    expires_at timestamp without time zone NOT NULL,
    max_ingested_at timestamp without time zone,
    max_sequence integer,
    max_event_id character varying(80),
    item_count integer NOT NULL,
    payload_bytes integer NOT NULL,
    payload_json json NOT NULL,
    created_at timestamp without time zone NOT NULL,
    CONSTRAINT ck_obs30_snapshot_expiry CHECK ((expires_at > created_at)),
    CONSTRAINT ck_obs30_snapshot_item_count CHECK ((item_count >= 0)),
    CONSTRAINT ck_obs30_snapshot_payload_bytes CHECK ((payload_bytes >= 0))
);


--
-- Name: tuge_obs30_scope_jti_claim; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tuge_obs30_scope_jti_claim (
    claim_hash character varying(64) NOT NULL,
    service_subject_hash character varying(64) NOT NULL,
    expires_at timestamp without time zone NOT NULL,
    claimed_at timestamp without time zone NOT NULL,
    CONSTRAINT ck_obs30_scope_jti_claim_expiry CHECK ((expires_at > claimed_at))
);


--
-- Name: tuge_security_audit_event; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tuge_security_audit_event (
    id character varying(80) NOT NULL,
    idempotency_key character varying(96) NOT NULL,
    immutable_fingerprint character varying(64) NOT NULL,
    audit_action_id character varying(96) NOT NULL,
    access_session_id character varying(96),
    audit_layer character varying(32) NOT NULL,
    parent_audit_event_id character varying(96),
    scope_type character varying(16) NOT NULL,
    schema_version integer NOT NULL,
    service character varying(80) NOT NULL,
    tenant_id character varying(64),
    occurred_at timestamp without time zone NOT NULL,
    ingested_at timestamp without time zone NOT NULL,
    category character varying(80) NOT NULL,
    action character varying(120) NOT NULL,
    risk_level character varying(16) NOT NULL,
    actor_type character varying(16),
    actor_id character varying(120),
    subject_type character varying(80),
    subject_id character varying(120),
    missing_context_reason text,
    cross_tenant boolean NOT NULL,
    reason_code character varying(80),
    source_ip_masked character varying(80),
    request_id character varying(120),
    trace_id character varying(64),
    outcome character varying(32) NOT NULL,
    error_code character varying(120),
    display_code character varying(120) NOT NULL,
    metadata_json json NOT NULL,
    CONSTRAINT ck_obs30_security_actor CHECK (((actor_type IS NULL) OR ((actor_type)::text = ANY ((ARRAY['USER'::character varying, 'SERVICE'::character varying, 'SYSTEM'::character varying, 'ANONYMOUS'::character varying, 'UNKNOWN'::character varying])::text[])))),
    CONSTRAINT ck_obs30_security_layer CHECK (((audit_layer)::text = 'PYTHON_EXECUTION'::text)),
    CONSTRAINT ck_obs30_security_outcome CHECK (((outcome)::text = ANY ((ARRAY['SUCCESS'::character varying, 'FAILURE'::character varying, 'DENIED'::character varying, 'CANCELLED'::character varying, 'TIMEOUT'::character varying, 'PARTIAL'::character varying, 'UNKNOWN'::character varying, 'ABANDONED'::character varying])::text[]))),
    CONSTRAINT ck_obs30_security_risk CHECK (((risk_level)::text = ANY ((ARRAY['LOW'::character varying, 'MEDIUM'::character varying, 'HIGH'::character varying, 'CRITICAL'::character varying])::text[]))),
    CONSTRAINT ck_obs30_security_scope CHECK (((scope_type)::text = ANY ((ARRAY['TENANT'::character varying, 'SYSTEM'::character varying])::text[]))),
    CONSTRAINT ck_obs30_security_tenant_scope CHECK (((((scope_type)::text = 'TENANT'::text) AND (tenant_id IS NOT NULL)) OR ((scope_type)::text = 'SYSTEM'::text)))
);


--
-- Name: tuge_skill_package; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tuge_skill_package (
    tenant_id character varying(64),
    package_name character varying(80) NOT NULL,
    display_name character varying(120) NOT NULL,
    description character varying NOT NULL,
    tags_json character varying NOT NULL,
    primary_skill character varying(120) NOT NULL,
    auxiliary_skills_json character varying NOT NULL,
    enabled boolean NOT NULL,
    created_at timestamp without time zone NOT NULL,
    updated_at timestamp without time zone NOT NULL,
    id character varying(64) NOT NULL
);


--
-- Name: tuge_task; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tuge_task (
    id character varying(80) NOT NULL,
    parent_task_id character varying(80),
    root_task_id character varying(80),
    task_key character varying(160),
    idempotency_key character varying(160),
    request_fingerprint character varying(64) NOT NULL,
    service character varying(80) NOT NULL,
    task_type character varying(120) NOT NULL,
    status character varying(32) NOT NULL,
    title text,
    handler_name character varying(80) NOT NULL,
    input_payload_json json,
    result_payload_json json,
    error_payload_json json,
    definition_snapshot_json json,
    output_schema_json json,
    agent_id character varying(80) NOT NULL,
    model_pack_id character varying(120) NOT NULL,
    thread_id character varying(80),
    session_id character varying(160),
    user_id character varying(120) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    stream_mode boolean NOT NULL,
    current_run_id character varying(80),
    attempt_count integer NOT NULL,
    progress_current integer NOT NULL,
    progress_total integer NOT NULL,
    cancel_requested boolean NOT NULL,
    priority integer NOT NULL,
    created_at timestamp without time zone,
    started_at timestamp without time zone,
    finished_at timestamp without time zone,
    expires_at timestamp without time zone,
    updated_at timestamp without time zone,
    metadata_json json
);


--
-- Name: tuge_task_artifact; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tuge_task_artifact (
    id character varying(80) NOT NULL,
    task_id character varying(80) NOT NULL,
    run_id character varying(80) NOT NULL,
    stage_run_id character varying(80) NOT NULL,
    artifact_type character varying(120) NOT NULL,
    artifact_version integer NOT NULL,
    schema_name character varying(120) NOT NULL,
    schema_version character varying(32) NOT NULL,
    content_json json,
    content_uri text,
    summary text,
    parent_artifact_ids_json json,
    checksum character varying(64) NOT NULL,
    metadata_json json,
    created_at timestamp without time zone
);


--
-- Name: tuge_task_event; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tuge_task_event (
    id character varying(80) NOT NULL,
    task_id character varying(80) NOT NULL,
    run_id character varying(80),
    schema_version character varying(16) NOT NULL,
    parent_event_id character varying(80),
    sequence integer NOT NULL,
    event_type character varying(80) NOT NULL,
    level character varying(20) NOT NULL,
    stage character varying(80) NOT NULL,
    step_id character varying(120),
    step_index integer,
    item_id character varying(80),
    stage_run_id character varying(80),
    agent_id character varying(80),
    tool_call_id character varying(120),
    stream_semantics character varying(24) NOT NULL,
    source_json json,
    duration_ms integer,
    token_usage_json json,
    error_code character varying(80),
    visible boolean NOT NULL,
    message text,
    payload_json json,
    tenant_id character varying(64),
    user_id character varying(120),
    request_id character varying(120),
    trace_id character varying(64),
    span_id character varying(32),
    privacy_mode character varying(16),
    route_type character varying(16),
    service_name character varying(80),
    service_version character varying(80),
    environment character varying(32),
    occurred_at timestamp without time zone,
    ingested_at timestamp without time zone,
    created_at timestamp without time zone
);


--
-- Name: tuge_task_item; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tuge_task_item (
    id character varying(80) NOT NULL,
    task_id character varying(80) NOT NULL,
    run_id character varying(80),
    item_type character varying(80) NOT NULL,
    item_key character varying(160) NOT NULL,
    sequence integer NOT NULL,
    status character varying(32) NOT NULL,
    input_payload_json json,
    result_payload_json json,
    error_payload_json json,
    created_at timestamp without time zone,
    started_at timestamp without time zone,
    finished_at timestamp without time zone,
    updated_at timestamp without time zone
);


--
-- Name: tuge_task_memory; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tuge_task_memory (
    id character varying(80) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    user_id character varying(120) NOT NULL,
    task_key character varying(160) NOT NULL,
    version integer NOT NULL,
    content text,
    source_text text,
    created_at timestamp without time zone
);


--
-- Name: tuge_task_quota; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tuge_task_quota (
    id character varying(80) NOT NULL,
    service character varying(80) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    resource_pool character varying(80) NOT NULL,
    max_concurrency integer NOT NULL,
    running_count integer NOT NULL,
    last_scheduled_at timestamp without time zone,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
);


--
-- Name: tuge_task_run; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tuge_task_run (
    id character varying(80) NOT NULL,
    task_id character varying(80) NOT NULL,
    idempotency_key character varying(160),
    request_fingerprint character varying(64) NOT NULL,
    pipeline_id character varying(120) NOT NULL,
    pipeline_version character varying(32) NOT NULL,
    model_pack_id character varying(120) NOT NULL,
    status character varying(32) NOT NULL,
    outcome character varying(32),
    current_stage_id character varying(120),
    cancel_requested boolean NOT NULL,
    pause_requested boolean NOT NULL,
    warning_count integer NOT NULL,
    error_code character varying(120),
    error_message text,
    lease_owner character varying(120),
    lease_until timestamp without time zone,
    lease_version integer NOT NULL,
    last_heartbeat_at timestamp without time zone,
    quota_slot_released boolean NOT NULL,
    resource_pool character varying(80) NOT NULL,
    resource_access_mode character varying(8),
    next_event_sequence integer NOT NULL,
    metadata_json json,
    started_at timestamp without time zone,
    finished_at timestamp without time zone,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
);


--
-- Name: tuge_task_stage_run; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tuge_task_stage_run (
    id character varying(80) NOT NULL,
    task_id character varying(80) NOT NULL,
    run_id character varying(80) NOT NULL,
    stage_id character varying(120) NOT NULL,
    stage_type character varying(32) NOT NULL,
    attempt integer NOT NULL,
    status character varying(32) NOT NULL,
    agent_id character varying(80),
    thread_id character varying(80),
    session_id character varying(160),
    input_artifact_ids_json json,
    output_artifact_id character varying(80),
    error_code character varying(120),
    error_message text,
    metadata_json json,
    started_at timestamp without time zone,
    finished_at timestamp without time zone,
    duration_ms integer,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
);


--
-- Name: tuge_task_user_schedule; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tuge_task_user_schedule (
    id character varying(80) NOT NULL,
    service character varying(80) NOT NULL,
    tenant_id character varying(64) NOT NULL,
    resource_pool character varying(80) NOT NULL,
    user_id character varying(120) NOT NULL,
    last_scheduled_at timestamp without time zone,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
);


--
-- Name: tuge_thread; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tuge_thread (
    id character varying NOT NULL,
    user_id character varying NOT NULL,
    title text,
    tenant_id character varying,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
);


--
-- Name: tuge_tool_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tuge_tool_config (
    tenant_id character varying(64),
    tool_name character varying(64) NOT NULL,
    provider character varying(64) NOT NULL,
    enabled boolean NOT NULL,
    config_json character varying NOT NULL,
    encrypted_secrets_json character varying NOT NULL,
    id character varying(64) NOT NULL
);


--
-- Name: tuge_agent_profile tuge_agent_profile_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_agent_profile
    ADD CONSTRAINT tuge_agent_profile_pkey PRIMARY KEY (agent_id);


--
-- Name: tuge_attachment_chunk tuge_attachment_chunk_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_attachment_chunk
    ADD CONSTRAINT tuge_attachment_chunk_pkey PRIMARY KEY (id);


--
-- Name: tuge_attachment tuge_attachment_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_attachment
    ADD CONSTRAINT tuge_attachment_pkey PRIMARY KEY (id);


--
-- Name: tuge_attachment_reference_owner tuge_attachment_reference_owner_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_attachment_reference_owner
    ADD CONSTRAINT tuge_attachment_reference_owner_pkey PRIMARY KEY (tenant_id, owner_id);


--
-- Name: tuge_attachment_text_content tuge_attachment_text_content_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_attachment_text_content
    ADD CONSTRAINT tuge_attachment_text_content_pkey PRIMARY KEY (file_id);


--
-- Name: tuge_attachment_upload_session tuge_attachment_upload_session_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_attachment_upload_session
    ADD CONSTRAINT tuge_attachment_upload_session_pkey PRIMARY KEY (id);


--
-- Name: tuge_discussion_participant tuge_discussion_participant_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_discussion_participant
    ADD CONSTRAINT tuge_discussion_participant_pkey PRIMARY KEY (id);


--
-- Name: tuge_discussion_run tuge_discussion_run_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_discussion_run
    ADD CONSTRAINT tuge_discussion_run_pkey PRIMARY KEY (id);


--
-- Name: tuge_discussion_turn tuge_discussion_turn_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_discussion_turn
    ADD CONSTRAINT tuge_discussion_turn_pkey PRIMARY KEY (id);


--
-- Name: tuge_llm_model tuge_llm_model_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_llm_model
    ADD CONSTRAINT tuge_llm_model_pkey PRIMARY KEY (id);


--
-- Name: tuge_message tuge_message_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_message
    ADD CONSTRAINT tuge_message_pkey PRIMARY KEY (id);


--
-- Name: tuge_obs30_query_snapshot tuge_obs30_query_snapshot_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_obs30_query_snapshot
    ADD CONSTRAINT tuge_obs30_query_snapshot_pkey PRIMARY KEY (handle);


--
-- Name: tuge_obs30_query_snapshot tuge_obs30_query_snapshot_query_snapshot_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_obs30_query_snapshot
    ADD CONSTRAINT tuge_obs30_query_snapshot_query_snapshot_id_key UNIQUE (query_snapshot_id);


--
-- Name: tuge_obs30_scope_jti_claim tuge_obs30_scope_jti_claim_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_obs30_scope_jti_claim
    ADD CONSTRAINT tuge_obs30_scope_jti_claim_pkey PRIMARY KEY (claim_hash);


--
-- Name: tuge_security_audit_event tuge_security_audit_event_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_security_audit_event
    ADD CONSTRAINT tuge_security_audit_event_pkey PRIMARY KEY (id);


--
-- Name: tuge_skill_package tuge_skill_package_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_skill_package
    ADD CONSTRAINT tuge_skill_package_pkey PRIMARY KEY (id);


--
-- Name: tuge_task_artifact tuge_task_artifact_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_task_artifact
    ADD CONSTRAINT tuge_task_artifact_pkey PRIMARY KEY (id);


--
-- Name: tuge_task_event tuge_task_event_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_task_event
    ADD CONSTRAINT tuge_task_event_pkey PRIMARY KEY (id);


--
-- Name: tuge_task_item tuge_task_item_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_task_item
    ADD CONSTRAINT tuge_task_item_pkey PRIMARY KEY (id);


--
-- Name: tuge_task_memory tuge_task_memory_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_task_memory
    ADD CONSTRAINT tuge_task_memory_pkey PRIMARY KEY (id);


--
-- Name: tuge_task tuge_task_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_task
    ADD CONSTRAINT tuge_task_pkey PRIMARY KEY (id);


--
-- Name: tuge_task_quota tuge_task_quota_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_task_quota
    ADD CONSTRAINT tuge_task_quota_pkey PRIMARY KEY (id);


--
-- Name: tuge_task_run tuge_task_run_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_task_run
    ADD CONSTRAINT tuge_task_run_pkey PRIMARY KEY (id);


--
-- Name: tuge_task_stage_run tuge_task_stage_run_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_task_stage_run
    ADD CONSTRAINT tuge_task_stage_run_pkey PRIMARY KEY (id);


--
-- Name: tuge_task_user_schedule tuge_task_user_schedule_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_task_user_schedule
    ADD CONSTRAINT tuge_task_user_schedule_pkey PRIMARY KEY (id);


--
-- Name: tuge_thread tuge_thread_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_thread
    ADD CONSTRAINT tuge_thread_pkey PRIMARY KEY (id);


--
-- Name: tuge_tool_config tuge_tool_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_tool_config
    ADD CONSTRAINT tuge_tool_config_pkey PRIMARY KEY (id);


--
-- Name: tuge_llm_model unique_tuge_llm_model; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_llm_model
    ADD CONSTRAINT unique_tuge_llm_model UNIQUE (tenant_id, provider_name, model_id);


--
-- Name: tuge_task_run unique_tuge_run_idempotency; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_task_run
    ADD CONSTRAINT unique_tuge_run_idempotency UNIQUE (task_id, idempotency_key);


--
-- Name: tuge_skill_package unique_tuge_skill_package; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_skill_package
    ADD CONSTRAINT unique_tuge_skill_package UNIQUE (tenant_id, package_name);


--
-- Name: tuge_task_stage_run unique_tuge_stage_attempt; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_task_stage_run
    ADD CONSTRAINT unique_tuge_stage_attempt UNIQUE (run_id, stage_id, attempt);


--
-- Name: tuge_task_memory unique_tuge_task_memory_version; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_task_memory
    ADD CONSTRAINT unique_tuge_task_memory_version UNIQUE (tenant_id, user_id, task_key, version);


--
-- Name: tuge_task_quota unique_tuge_task_quota_scope; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_task_quota
    ADD CONSTRAINT unique_tuge_task_quota_scope UNIQUE (service, tenant_id, resource_pool);


--
-- Name: tuge_task_user_schedule unique_tuge_task_user_schedule_scope; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_task_user_schedule
    ADD CONSTRAINT unique_tuge_task_user_schedule_scope UNIQUE (service, tenant_id, resource_pool, user_id);


--
-- Name: tuge_tool_config unique_tuge_tool_config; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_tool_config
    ADD CONSTRAINT unique_tuge_tool_config UNIQUE (tenant_id, tool_name, provider);


--
-- Name: tuge_attachment uq_file_tenant_alias; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_attachment
    ADD CONSTRAINT uq_file_tenant_alias UNIQUE (tenant_id, alias_id);


--
-- Name: tuge_attachment uq_file_tenant_md5_purpose; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_attachment
    ADD CONSTRAINT uq_file_tenant_md5_purpose UNIQUE (tenant_id, file_md5, purpose);


--
-- Name: tuge_security_audit_event uq_obs30_security_idempotency; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_security_audit_event
    ADD CONSTRAINT uq_obs30_security_idempotency UNIQUE (idempotency_key);


--
-- Name: idx_obs30_scope_jti_claim_expiry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_obs30_scope_jti_claim_expiry ON public.tuge_obs30_scope_jti_claim USING btree (expires_at);


--
-- Name: idx_obs30_scope_jti_claim_subject; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_obs30_scope_jti_claim_subject ON public.tuge_obs30_scope_jti_claim USING btree (service_subject_hash, claimed_at);


--
-- Name: idx_obs30_security_action; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_obs30_security_action ON public.tuge_security_audit_event USING btree (audit_action_id, occurred_at, id);


--
-- Name: idx_obs30_security_tenant_occurred; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_obs30_security_tenant_occurred ON public.tuge_security_audit_event USING btree (tenant_id, occurred_at, id);


--
-- Name: idx_obs30_snapshot_expiry; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_obs30_snapshot_expiry ON public.tuge_obs30_query_snapshot USING btree (expires_at);


--
-- Name: idx_obs30_snapshot_resource; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_obs30_snapshot_resource ON public.tuge_obs30_query_snapshot USING btree (resource_kind, created_at, handle);


--
-- Name: idx_obs30_snapshot_scope_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_obs30_snapshot_scope_created ON public.tuge_obs30_query_snapshot USING btree (scope_hash, created_at, handle);


--
-- Name: idx_tuge_task_event_task_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tuge_task_event_task_created ON public.tuge_task_event USING btree (task_id, created_at);


--
-- Name: idx_tuge_task_event_type_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tuge_task_event_type_created ON public.tuge_task_event USING btree (event_type, created_at);


--
-- Name: idx_tuge_task_quota_scope; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tuge_task_quota_scope ON public.tuge_task_quota USING btree (service, tenant_id, resource_pool);


--
-- Name: idx_tuge_task_run_claim; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tuge_task_run_claim ON public.tuge_task_run USING btree (status, lease_until, created_at);


--
-- Name: idx_tuge_task_run_resource_claim; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_tuge_task_run_resource_claim ON public.tuge_task_run USING btree (resource_pool, resource_access_mode, status, created_at);


--
-- Name: ix_file_chunk_tenant_file; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_file_chunk_tenant_file ON public.tuge_attachment_chunk USING btree (tenant_id, file_id);


--
-- Name: ix_file_chunk_tenant_file_index; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_file_chunk_tenant_file_index ON public.tuge_attachment_chunk USING btree (tenant_id, file_id, chunk_index);


--
-- Name: ix_file_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_file_expires_at ON public.tuge_attachment USING btree (expires_at);


--
-- Name: ix_file_tenant_purpose_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_file_tenant_purpose_created ON public.tuge_attachment USING btree (tenant_id, purpose, created_at);


--
-- Name: ix_file_upload_session_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_file_upload_session_expires_at ON public.tuge_attachment_upload_session USING btree (expires_at);


--
-- Name: ix_file_upload_session_tenant_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_file_upload_session_tenant_status ON public.tuge_attachment_upload_session USING btree (tenant_id, status);


--
-- Name: ix_tuge_attachment_chunk_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tuge_attachment_chunk_tenant_id ON public.tuge_attachment_chunk USING btree (tenant_id);


--
-- Name: ix_tuge_attachment_ref_count; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tuge_attachment_ref_count ON public.tuge_attachment USING btree (ref_count);


--
-- Name: ix_tuge_attachment_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tuge_attachment_tenant_id ON public.tuge_attachment USING btree (tenant_id);


--
-- Name: ix_tuge_attachment_text_content_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tuge_attachment_text_content_tenant_id ON public.tuge_attachment_text_content USING btree (tenant_id);


--
-- Name: ix_tuge_attachment_upload_session_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tuge_attachment_upload_session_tenant_id ON public.tuge_attachment_upload_session USING btree (tenant_id);


--
-- Name: ix_tuge_task_artifact_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tuge_task_artifact_run_id ON public.tuge_task_artifact USING btree (run_id);


--
-- Name: ix_tuge_task_artifact_stage_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tuge_task_artifact_stage_run_id ON public.tuge_task_artifact USING btree (stage_run_id);


--
-- Name: ix_tuge_task_artifact_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tuge_task_artifact_task_id ON public.tuge_task_artifact USING btree (task_id);


--
-- Name: ix_tuge_task_memory_task_key; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tuge_task_memory_task_key ON public.tuge_task_memory USING btree (task_key);


--
-- Name: ix_tuge_task_memory_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tuge_task_memory_tenant_id ON public.tuge_task_memory USING btree (tenant_id);


--
-- Name: ix_tuge_task_memory_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tuge_task_memory_user_id ON public.tuge_task_memory USING btree (user_id);


--
-- Name: ix_tuge_task_run_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tuge_task_run_task_id ON public.tuge_task_run USING btree (task_id);


--
-- Name: ix_tuge_task_stage_run_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tuge_task_stage_run_run_id ON public.tuge_task_stage_run USING btree (run_id);


--
-- Name: ix_tuge_task_stage_run_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tuge_task_stage_run_task_id ON public.tuge_task_stage_run USING btree (task_id);


--
-- Name: uq_tuge_task_event_run_sequence; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_tuge_task_event_run_sequence ON public.tuge_task_event USING btree (run_id, sequence) WHERE (run_id IS NOT NULL);


--
-- Name: uq_tuge_task_service_tenant_idempotency; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_tuge_task_service_tenant_idempotency ON public.tuge_task USING btree (service, tenant_id, idempotency_key);


--
-- Name: tuge_discussion_participant tuge_discussion_participant_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_discussion_participant
    ADD CONSTRAINT tuge_discussion_participant_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.tuge_discussion_run(id);


--
-- Name: tuge_discussion_turn tuge_discussion_turn_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_discussion_turn
    ADD CONSTRAINT tuge_discussion_turn_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.tuge_discussion_run(id);


--
-- Name: tuge_message tuge_message_thread_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_message
    ADD CONSTRAINT tuge_message_thread_id_fkey FOREIGN KEY (thread_id) REFERENCES public.tuge_thread(id) ON DELETE CASCADE;


--
-- Name: tuge_task_artifact tuge_task_artifact_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_task_artifact
    ADD CONSTRAINT tuge_task_artifact_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.tuge_task_run(id);


--
-- Name: tuge_task_artifact tuge_task_artifact_stage_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_task_artifact
    ADD CONSTRAINT tuge_task_artifact_stage_run_id_fkey FOREIGN KEY (stage_run_id) REFERENCES public.tuge_task_stage_run(id);


--
-- Name: tuge_task_artifact tuge_task_artifact_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_task_artifact
    ADD CONSTRAINT tuge_task_artifact_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tuge_task(id);


--
-- Name: tuge_task_event tuge_task_event_parent_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_task_event
    ADD CONSTRAINT tuge_task_event_parent_event_id_fkey FOREIGN KEY (parent_event_id) REFERENCES public.tuge_task_event(id);


--
-- Name: tuge_task_event tuge_task_event_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_task_event
    ADD CONSTRAINT tuge_task_event_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tuge_task(id);


--
-- Name: tuge_task_item tuge_task_item_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_task_item
    ADD CONSTRAINT tuge_task_item_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tuge_task(id);


--
-- Name: tuge_task tuge_task_parent_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_task
    ADD CONSTRAINT tuge_task_parent_task_id_fkey FOREIGN KEY (parent_task_id) REFERENCES public.tuge_task(id);


--
-- Name: tuge_task_run tuge_task_run_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_task_run
    ADD CONSTRAINT tuge_task_run_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tuge_task(id);


--
-- Name: tuge_task_stage_run tuge_task_stage_run_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_task_stage_run
    ADD CONSTRAINT tuge_task_stage_run_run_id_fkey FOREIGN KEY (run_id) REFERENCES public.tuge_task_run(id);


--
-- Name: tuge_task_stage_run tuge_task_stage_run_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tuge_task_stage_run
    ADD CONSTRAINT tuge_task_stage_run_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tuge_task(id);


--
-- PostgreSQL database dump complete
--
