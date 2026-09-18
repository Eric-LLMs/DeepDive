-- DeepDive canonical database initialization -- the single install script.
--
-- Fresh installs: the asyncpg runner (packages/core/infrastructure/db.py) applies
-- this file once inside a transaction and records "0001_init" in schema_migrations.
-- The development-time incremental migrations 0002-0019 were deliberately squashed
-- into this final schema; existing databases that already applied them carry the
-- "0001_init" version and skip re-application. Do not add dev-history deltas here
-- as new numbered files -- extend this schema instead.
--
-- Generated from the live PostgreSQL 16 schema (pg_dump --schema-only), plus the
-- curated reference seeds at the bottom.

--
-- PostgreSQL database dump
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

--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;


--
-- Name: EXTENSION pg_trgm; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION pg_trgm IS 'text similarity measurement and index searching based on trigrams';


--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;


--
-- Name: EXTENSION vector; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON EXTENSION vector IS 'vector data type and ivfflat and hnsw access methods';


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: access_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.access_tokens (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    credential_id uuid
);


--
-- Name: app_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.app_settings (
    key text NOT NULL,
    value jsonb NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: articles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.articles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    domain_id uuid,
    title text NOT NULL,
    content text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: asset_acl; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.asset_acl (
    asset_id uuid NOT NULL,
    grantee_user_id uuid,
    permission text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL
);


--
-- Name: assets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.assets (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    workspace_id uuid,
    object_sha256 text,
    name text NOT NULL,
    folder_path text,
    mime_type text,
    size bigint,
    file_status text DEFAULT 'uploading'::text NOT NULL,
    rag_status text DEFAULT 'pending'::text NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    deleted_at timestamp with time zone,
    domain_id uuid,
    source_asset_id uuid
);


--
-- Name: chunks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chunks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    asset_id uuid,
    user_id uuid,
    workspace_id uuid,
    seq integer NOT NULL,
    content_en text NOT NULL,
    content_cn text,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    embedding public.vector(1024) NOT NULL,
    parent_chunk_id uuid,
    chunk_kind text DEFAULT 'leaf'::text NOT NULL,
    content_search text,
    source_type text DEFAULT 'file'::text NOT NULL,
    source_id text
);


--
-- Name: credential_models; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.credential_models (
    credential_id uuid NOT NULL,
    model_id uuid NOT NULL,
    note text,
    priority integer DEFAULT 0 NOT NULL,
    weight integer DEFAULT 1 NOT NULL,
    prompt_price_per_1k numeric(12,6),
    completion_price_per_1k numeric(12,6),
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: domains; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.domains (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    user_id uuid
);


--
-- Name: folders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.folders (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    workspace_id uuid,
    path text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: global_objects; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.global_objects (
    sha256 text NOT NULL,
    size bigint NOT NULL,
    storage_key text NOT NULL,
    mime_type text,
    ref_count bigint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.jobs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    type text NOT NULL,
    status text NOT NULL,
    payload jsonb NOT NULL,
    result jsonb,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    user_id uuid
);


--
-- Name: llm_credentials; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_credentials (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    base_url text NOT NULL,
    api_key text NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone
);


--
-- Name: llm_models; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_models (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name text NOT NULL,
    description text,
    prompt_price_per_1k numeric(12,6) DEFAULT 0 NOT NULL,
    completion_price_per_1k numeric(12,6) DEFAULT 0 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    provider_model_name text
);


--
-- Name: login_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.login_tokens (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid,
    name text NOT NULL,
    token_hash text NOT NULL,
    role text DEFAULT 'user'::text NOT NULL,
    role_id text,
    credential_id uuid,
    expires_at timestamp with time zone,
    last_used_at timestamp with time zone,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: matches; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.matches (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    term_id uuid NOT NULL,
    sentence_id uuid NOT NULL,
    cn_explanation text
);


--
-- Name: messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.messages (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    session_id uuid NOT NULL,
    role text NOT NULL,
    text text NOT NULL,
    embedding public.vector(1024),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    imported_rag boolean DEFAULT false NOT NULL,
    attach_asset_id uuid,
    meta jsonb
);


--
-- Name: rag_feedback; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.rag_feedback (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid,
    query text NOT NULL,
    rating boolean NOT NULL,
    reason text,
    hits jsonb DEFAULT '[]'::jsonb NOT NULL,
    filters jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: role_credentials; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.role_credentials (
    role_id text NOT NULL,
    credential_id uuid NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: sentences; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sentences (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    domain_id uuid NOT NULL,
    origin_source text,
    content_en text NOT NULL,
    content_cn text,
    audio_hash text,
    cn_explanation text,
    embedding public.vector(1024),
    user_id uuid
);


--
-- Name: session_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.session_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    session_id uuid NOT NULL,
    seq integer NOT NULL,
    type text NOT NULL,
    "timestamp" double precision NOT NULL,
    payload jsonb NOT NULL
);


--
-- Name: sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    closed_at timestamp with time zone,
    summary text,
    title text,
    type integer DEFAULT 0 NOT NULL,
    compaction jsonb
);


--
-- Name: terms; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.terms (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    domain_id uuid NOT NULL,
    word text NOT NULL,
    definition text,
    frequency integer NOT NULL,
    star_level integer NOT NULL,
    audio_hash text,
    image_paths jsonb NOT NULL,
    is_active boolean NOT NULL,
    user_id uuid
);


--
-- Name: upload_sessions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.upload_sessions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    asset_id uuid NOT NULL,
    sha256 text NOT NULL,
    size bigint NOT NULL,
    chunk_size integer NOT NULL,
    num_chunks integer NOT NULL,
    received_chunks jsonb DEFAULT '[]'::jsonb NOT NULL,
    status text DEFAULT 'pending'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: user_roles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_roles (
    role_id text NOT NULL,
    role_name text NOT NULL,
    daily_request_limit integer DEFAULT 50 NOT NULL,
    monthly_request_limit integer DEFAULT 1500 NOT NULL,
    daily_token_limit bigint DEFAULT '-1'::integer,
    rpm_limit integer DEFAULT '-1'::integer,
    monthly_cost_limit numeric(12,6) DEFAULT '-1'::integer,
    default_model text DEFAULT ''::text,
    models text[] DEFAULT '{}'::text[],
    features jsonb DEFAULT '{}'::jsonb NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: user_usage_counters; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_usage_counters (
    user_id uuid NOT NULL,
    period_type text NOT NULL,
    period_start date NOT NULL,
    request_count bigint DEFAULT 0 NOT NULL,
    token_count bigint DEFAULT 0 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: user_usage_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_usage_logs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid,
    token_id uuid,
    role_id text,
    model_name text,
    tool text,
    prompt_tokens integer DEFAULT 0 NOT NULL,
    completion_tokens integer DEFAULT 0 NOT NULL,
    total_tokens integer DEFAULT 0 NOT NULL,
    cost_usd numeric(12,6) DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    credential_id uuid
);


--
-- Name: user_wallets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_wallets (
    user_id uuid NOT NULL,
    balance numeric(14,6) DEFAULT 0 NOT NULL,
    currency text DEFAULT 'USD'::text NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    username text,
    password_hash text,
    display_name text,
    is_active boolean DEFAULT true NOT NULL,
    role_id text DEFAULT 'regular'::text NOT NULL,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    updated_at timestamp with time zone,
    email text,
    phone text,
    avatar text,
    email_verified boolean DEFAULT false NOT NULL
);


--
-- Name: verification_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.verification_tokens (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    kind text NOT NULL,
    token_hash text NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: wallet_transactions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.wallet_transactions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    type text NOT NULL,
    amount numeric(14,6) NOT NULL,
    balance_after numeric(14,6) NOT NULL,
    description text,
    meta jsonb DEFAULT '{}'::jsonb NOT NULL,
    idempotency_key text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: workspace_activity; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workspace_activity (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    workspace_id uuid,
    actor_user_id uuid,
    actor_username text,
    action text NOT NULL,
    target_type text NOT NULL,
    target_id text,
    target_name text,
    detail text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: workspace_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workspace_members (
    workspace_id uuid NOT NULL,
    user_id uuid NOT NULL,
    role text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: workspaces; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workspaces (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    owner_id uuid NOT NULL,
    name text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: access_tokens access_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.access_tokens
    ADD CONSTRAINT access_tokens_pkey PRIMARY KEY (id);


--
-- Name: app_settings app_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_settings
    ADD CONSTRAINT app_settings_pkey PRIMARY KEY (key);


--
-- Name: articles articles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.articles
    ADD CONSTRAINT articles_pkey PRIMARY KEY (id);


--
-- Name: asset_acl asset_acl_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_acl
    ADD CONSTRAINT asset_acl_pkey PRIMARY KEY (id);


--
-- Name: assets assets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assets
    ADD CONSTRAINT assets_pkey PRIMARY KEY (id);


--
-- Name: chunks chunks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_pkey PRIMARY KEY (id);


--
-- Name: credential_models credential_models_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credential_models
    ADD CONSTRAINT credential_models_pkey PRIMARY KEY (credential_id, model_id);


--
-- Name: domains domains_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.domains
    ADD CONSTRAINT domains_pkey PRIMARY KEY (id);


--
-- Name: folders folders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.folders
    ADD CONSTRAINT folders_pkey PRIMARY KEY (id);


--
-- Name: global_objects global_objects_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.global_objects
    ADD CONSTRAINT global_objects_pkey PRIMARY KEY (sha256);


--
-- Name: jobs jobs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_pkey PRIMARY KEY (id);


--
-- Name: llm_credentials llm_credentials_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_credentials
    ADD CONSTRAINT llm_credentials_pkey PRIMARY KEY (id);


--
-- Name: llm_models llm_models_name_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_models
    ADD CONSTRAINT llm_models_name_key UNIQUE (name);


--
-- Name: llm_models llm_models_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_models
    ADD CONSTRAINT llm_models_pkey PRIMARY KEY (id);


--
-- Name: login_tokens login_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.login_tokens
    ADD CONSTRAINT login_tokens_pkey PRIMARY KEY (id);


--
-- Name: login_tokens login_tokens_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.login_tokens
    ADD CONSTRAINT login_tokens_token_hash_key UNIQUE (token_hash);


--
-- Name: matches matches_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.matches
    ADD CONSTRAINT matches_pkey PRIMARY KEY (id);


--
-- Name: messages messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_pkey PRIMARY KEY (id);


--
-- Name: rag_feedback rag_feedback_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rag_feedback
    ADD CONSTRAINT rag_feedback_pkey PRIMARY KEY (id);


--
-- Name: role_credentials role_credentials_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role_credentials
    ADD CONSTRAINT role_credentials_pkey PRIMARY KEY (role_id, credential_id);


--
-- Name: sentences sentences_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sentences
    ADD CONSTRAINT sentences_pkey PRIMARY KEY (id);


--
-- Name: session_events session_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_events
    ADD CONSTRAINT session_events_pkey PRIMARY KEY (id);


--
-- Name: sessions sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_pkey PRIMARY KEY (id);


--
-- Name: terms terms_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.terms
    ADD CONSTRAINT terms_pkey PRIMARY KEY (id);


--
-- Name: upload_sessions upload_sessions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.upload_sessions
    ADD CONSTRAINT upload_sessions_pkey PRIMARY KEY (id);


--
-- Name: user_roles user_roles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_roles
    ADD CONSTRAINT user_roles_pkey PRIMARY KEY (role_id);


--
-- Name: user_usage_counters user_usage_counters_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_usage_counters
    ADD CONSTRAINT user_usage_counters_pkey PRIMARY KEY (user_id, period_type, period_start);


--
-- Name: user_usage_logs user_usage_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_usage_logs
    ADD CONSTRAINT user_usage_logs_pkey PRIMARY KEY (id);


--
-- Name: user_wallets user_wallets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_wallets
    ADD CONSTRAINT user_wallets_pkey PRIMARY KEY (user_id);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: verification_tokens verification_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.verification_tokens
    ADD CONSTRAINT verification_tokens_pkey PRIMARY KEY (id);


--
-- Name: verification_tokens verification_tokens_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.verification_tokens
    ADD CONSTRAINT verification_tokens_token_hash_key UNIQUE (token_hash);


--
-- Name: wallet_transactions wallet_transactions_idempotency_key_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_transactions
    ADD CONSTRAINT wallet_transactions_idempotency_key_key UNIQUE (idempotency_key);


--
-- Name: wallet_transactions wallet_transactions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_transactions
    ADD CONSTRAINT wallet_transactions_pkey PRIMARY KEY (id);


--
-- Name: workspace_activity workspace_activity_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_activity
    ADD CONSTRAINT workspace_activity_pkey PRIMARY KEY (id);


--
-- Name: workspace_members workspace_members_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_members
    ADD CONSTRAINT workspace_members_pkey PRIMARY KEY (workspace_id, user_id);


--
-- Name: workspaces workspaces_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspaces
    ADD CONSTRAINT workspaces_pkey PRIMARY KEY (id);


--
-- Name: access_tokens_credential_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX access_tokens_credential_id_idx ON public.access_tokens USING btree (credential_id);


--
-- Name: access_tokens_user_credential_uniq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX access_tokens_user_credential_uniq ON public.access_tokens USING btree (user_id, credential_id) WHERE ((user_id IS NOT NULL) AND (credential_id IS NOT NULL));


--
-- Name: access_tokens_user_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX access_tokens_user_id_idx ON public.access_tokens USING btree (user_id);


--
-- Name: asset_acl_grantee_uniq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX asset_acl_grantee_uniq ON public.asset_acl USING btree (asset_id, grantee_user_id) WHERE (grantee_user_id IS NOT NULL);


--
-- Name: asset_acl_public_uniq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX asset_acl_public_uniq ON public.asset_acl USING btree (asset_id) WHERE (grantee_user_id IS NULL);


--
-- Name: assets_domain_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX assets_domain_idx ON public.assets USING btree (domain_id);


--
-- Name: assets_folder_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX assets_folder_idx ON public.assets USING btree (folder_path);


--
-- Name: assets_name_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX assets_name_idx ON public.assets USING btree (lower(name));


--
-- Name: assets_object_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX assets_object_idx ON public.assets USING btree (object_sha256);


--
-- Name: assets_user_deleted_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX assets_user_deleted_idx ON public.assets USING btree (user_id, deleted_at);


--
-- Name: assets_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX assets_user_idx ON public.assets USING btree (user_id);


--
-- Name: assets_workspace_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX assets_workspace_idx ON public.assets USING btree (workspace_id);


--
-- Name: chunks_asset_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunks_asset_idx ON public.chunks USING btree (asset_id);


--
-- Name: chunks_content_search_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunks_content_search_idx ON public.chunks USING gin (to_tsvector('simple'::regconfig, COALESCE(content_search, ''::text)));


--
-- Name: chunks_embedding_hnsw; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunks_embedding_hnsw ON public.chunks USING hnsw (embedding public.vector_cosine_ops) WITH (m='16', ef_construction='64');


--
-- Name: chunks_parent_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunks_parent_idx ON public.chunks USING btree (parent_chunk_id);


--
-- Name: chunks_source_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunks_source_idx ON public.chunks USING btree (source_type);


--
-- Name: chunks_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunks_user_idx ON public.chunks USING btree (user_id);


--
-- Name: chunks_workspace_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chunks_workspace_idx ON public.chunks USING btree (workspace_id);


--
-- Name: domains_name_private_uniq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX domains_name_private_uniq ON public.domains USING btree (user_id, name) WHERE (user_id IS NOT NULL);


--
-- Name: domains_name_public_uniq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX domains_name_public_uniq ON public.domains USING btree (name) WHERE (user_id IS NULL);


--
-- Name: domains_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX domains_user_idx ON public.domains USING btree (user_id);


--
-- Name: folders_unique_personal; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX folders_unique_personal ON public.folders USING btree (path) WHERE (workspace_id IS NULL);


--
-- Name: folders_unique_ws; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX folders_unique_ws ON public.folders USING btree (workspace_id, path) WHERE (workspace_id IS NOT NULL);


--
-- Name: folders_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX folders_user_idx ON public.folders USING btree (user_id);


--
-- Name: idx_assets_source_asset; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_assets_source_asset ON public.assets USING btree (source_asset_id);


--
-- Name: idx_assets_source_content; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_assets_source_content ON public.assets USING btree (source_asset_id, object_sha256);


--
-- Name: idx_messages_attach_asset; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_messages_attach_asset ON public.messages USING btree (attach_asset_id);


--
-- Name: idx_session_events_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_session_events_timestamp ON public.session_events USING btree ("timestamp");


--
-- Name: jobs_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX jobs_user_idx ON public.jobs USING btree (user_id);


--
-- Name: login_tokens_credential_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX login_tokens_credential_id_idx ON public.login_tokens USING btree (credential_id);


--
-- Name: login_tokens_user_credential_uniq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX login_tokens_user_credential_uniq ON public.login_tokens USING btree (user_id, credential_id) WHERE ((user_id IS NOT NULL) AND (credential_id IS NOT NULL));


--
-- Name: login_tokens_user_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX login_tokens_user_id_idx ON public.login_tokens USING btree (user_id);


--
-- Name: login_tokens_user_no_cred_uniq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX login_tokens_user_no_cred_uniq ON public.login_tokens USING btree (user_id) WHERE ((user_id IS NOT NULL) AND (credential_id IS NULL));


--
-- Name: rag_feedback_rating_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX rag_feedback_rating_idx ON public.rag_feedback USING btree (rating);


--
-- Name: rag_feedback_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX rag_feedback_user_idx ON public.rag_feedback USING btree (user_id);


--
-- Name: role_credentials_credential_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX role_credentials_credential_idx ON public.role_credentials USING btree (credential_id);


--
-- Name: sentences_content_en_private_uniq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX sentences_content_en_private_uniq ON public.sentences USING btree (user_id, content_en) WHERE (user_id IS NOT NULL);


--
-- Name: sentences_content_en_public_uniq; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX sentences_content_en_public_uniq ON public.sentences USING btree (content_en) WHERE (user_id IS NULL);


--
-- Name: sentences_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sentences_user_idx ON public.sentences USING btree (user_id);


--
-- Name: terms_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX terms_user_idx ON public.terms USING btree (user_id);


--
-- Name: upload_sessions_asset_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX upload_sessions_asset_idx ON public.upload_sessions USING btree (asset_id);


--
-- Name: upload_sessions_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX upload_sessions_user_idx ON public.upload_sessions USING btree (user_id);


--
-- Name: user_usage_logs_cred_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_usage_logs_cred_idx ON public.user_usage_logs USING btree (credential_id);


--
-- Name: user_usage_logs_token_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_usage_logs_token_id_idx ON public.user_usage_logs USING btree (token_id, created_at);


--
-- Name: user_usage_logs_user_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_usage_logs_user_id_idx ON public.user_usage_logs USING btree (user_id, created_at);


--
-- Name: users_email_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX users_email_idx ON public.users USING btree (email) WHERE (email IS NOT NULL);


--
-- Name: users_username_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX users_username_idx ON public.users USING btree (username) WHERE (username IS NOT NULL);


--
-- Name: wallet_transactions_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX wallet_transactions_user_idx ON public.wallet_transactions USING btree (user_id, created_at);


--
-- Name: workspace_members_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX workspace_members_user_idx ON public.workspace_members USING btree (user_id);


--
-- Name: workspaces_owner_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX workspaces_owner_idx ON public.workspaces USING btree (owner_id);


--
-- Name: ws_activity_actor_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ws_activity_actor_idx ON public.workspace_activity USING btree (actor_username);


--
-- Name: ws_activity_target_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ws_activity_target_idx ON public.workspace_activity USING btree (target_name);


--
-- Name: ws_activity_ws_created_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ws_activity_ws_created_idx ON public.workspace_activity USING btree (workspace_id, created_at DESC);


--
-- Name: access_tokens access_tokens_credential_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.access_tokens
    ADD CONSTRAINT access_tokens_credential_id_fkey FOREIGN KEY (credential_id) REFERENCES public.llm_credentials(id) ON DELETE SET NULL;


--
-- Name: access_tokens access_tokens_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.access_tokens
    ADD CONSTRAINT access_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: articles articles_domain_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.articles
    ADD CONSTRAINT articles_domain_id_fkey FOREIGN KEY (domain_id) REFERENCES public.domains(id) ON DELETE SET NULL;


--
-- Name: articles articles_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.articles
    ADD CONSTRAINT articles_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: asset_acl asset_acl_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_acl
    ADD CONSTRAINT asset_acl_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: asset_acl asset_acl_grantee_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.asset_acl
    ADD CONSTRAINT asset_acl_grantee_user_id_fkey FOREIGN KEY (grantee_user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: assets assets_domain_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assets
    ADD CONSTRAINT assets_domain_id_fkey FOREIGN KEY (domain_id) REFERENCES public.domains(id) ON DELETE SET NULL;


--
-- Name: assets assets_object_sha256_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assets
    ADD CONSTRAINT assets_object_sha256_fkey FOREIGN KEY (object_sha256) REFERENCES public.global_objects(sha256);


--
-- Name: assets assets_source_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assets
    ADD CONSTRAINT assets_source_asset_id_fkey FOREIGN KEY (source_asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: assets assets_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assets
    ADD CONSTRAINT assets_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id);


--
-- Name: assets assets_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.assets
    ADD CONSTRAINT assets_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id);


--
-- Name: chunks chunks_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: chunks chunks_parent_chunk_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chunks
    ADD CONSTRAINT chunks_parent_chunk_id_fkey FOREIGN KEY (parent_chunk_id) REFERENCES public.chunks(id) ON DELETE SET NULL;


--
-- Name: credential_models credential_models_credential_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credential_models
    ADD CONSTRAINT credential_models_credential_id_fkey FOREIGN KEY (credential_id) REFERENCES public.llm_credentials(id) ON DELETE CASCADE;


--
-- Name: credential_models credential_models_model_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credential_models
    ADD CONSTRAINT credential_models_model_id_fkey FOREIGN KEY (model_id) REFERENCES public.llm_models(id) ON DELETE CASCADE;


--
-- Name: domains domains_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.domains
    ADD CONSTRAINT domains_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: folders folders_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.folders
    ADD CONSTRAINT folders_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: folders folders_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.folders
    ADD CONSTRAINT folders_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE CASCADE;


--
-- Name: jobs jobs_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.jobs
    ADD CONSTRAINT jobs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: login_tokens login_tokens_credential_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.login_tokens
    ADD CONSTRAINT login_tokens_credential_id_fkey FOREIGN KEY (credential_id) REFERENCES public.llm_credentials(id) ON DELETE SET NULL;


--
-- Name: login_tokens login_tokens_role_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.login_tokens
    ADD CONSTRAINT login_tokens_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.user_roles(role_id) ON DELETE SET NULL;


--
-- Name: login_tokens login_tokens_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.login_tokens
    ADD CONSTRAINT login_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: matches matches_sentence_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.matches
    ADD CONSTRAINT matches_sentence_id_fkey FOREIGN KEY (sentence_id) REFERENCES public.sentences(id) ON DELETE CASCADE;


--
-- Name: matches matches_term_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.matches
    ADD CONSTRAINT matches_term_id_fkey FOREIGN KEY (term_id) REFERENCES public.terms(id) ON DELETE CASCADE;


--
-- Name: messages messages_attach_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_attach_asset_id_fkey FOREIGN KEY (attach_asset_id) REFERENCES public.assets(id) ON DELETE SET NULL;


--
-- Name: messages messages_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE;


--
-- Name: messages messages_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.messages
    ADD CONSTRAINT messages_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: rag_feedback rag_feedback_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rag_feedback
    ADD CONSTRAINT rag_feedback_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: role_credentials role_credentials_credential_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role_credentials
    ADD CONSTRAINT role_credentials_credential_id_fkey FOREIGN KEY (credential_id) REFERENCES public.llm_credentials(id) ON DELETE CASCADE;


--
-- Name: role_credentials role_credentials_role_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.role_credentials
    ADD CONSTRAINT role_credentials_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.user_roles(role_id) ON DELETE CASCADE;


--
-- Name: sentences sentences_domain_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sentences
    ADD CONSTRAINT sentences_domain_id_fkey FOREIGN KEY (domain_id) REFERENCES public.domains(id) ON DELETE CASCADE;


--
-- Name: sentences sentences_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sentences
    ADD CONSTRAINT sentences_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: session_events session_events_session_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.session_events
    ADD CONSTRAINT session_events_session_id_fkey FOREIGN KEY (session_id) REFERENCES public.sessions(id) ON DELETE CASCADE;


--
-- Name: sessions sessions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sessions
    ADD CONSTRAINT sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: terms terms_domain_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.terms
    ADD CONSTRAINT terms_domain_id_fkey FOREIGN KEY (domain_id) REFERENCES public.domains(id) ON DELETE CASCADE;


--
-- Name: terms terms_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.terms
    ADD CONSTRAINT terms_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: upload_sessions upload_sessions_asset_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.upload_sessions
    ADD CONSTRAINT upload_sessions_asset_id_fkey FOREIGN KEY (asset_id) REFERENCES public.assets(id) ON DELETE CASCADE;


--
-- Name: upload_sessions upload_sessions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.upload_sessions
    ADD CONSTRAINT upload_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: user_usage_counters user_usage_counters_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_usage_counters
    ADD CONSTRAINT user_usage_counters_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: user_usage_logs user_usage_logs_credential_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_usage_logs
    ADD CONSTRAINT user_usage_logs_credential_id_fkey FOREIGN KEY (credential_id) REFERENCES public.llm_credentials(id) ON DELETE SET NULL;


--
-- Name: user_usage_logs user_usage_logs_token_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_usage_logs
    ADD CONSTRAINT user_usage_logs_token_id_fkey FOREIGN KEY (token_id) REFERENCES public.login_tokens(id) ON DELETE SET NULL;


--
-- Name: user_usage_logs user_usage_logs_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_usage_logs
    ADD CONSTRAINT user_usage_logs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;


--
-- Name: user_wallets user_wallets_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_wallets
    ADD CONSTRAINT user_wallets_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: users users_role_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_role_id_fkey FOREIGN KEY (role_id) REFERENCES public.user_roles(role_id) ON DELETE RESTRICT;


--
-- Name: verification_tokens verification_tokens_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.verification_tokens
    ADD CONSTRAINT verification_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: wallet_transactions wallet_transactions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_transactions
    ADD CONSTRAINT wallet_transactions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: workspace_members workspace_members_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_members
    ADD CONSTRAINT workspace_members_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: workspace_members workspace_members_workspace_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspace_members
    ADD CONSTRAINT workspace_members_workspace_id_fkey FOREIGN KEY (workspace_id) REFERENCES public.workspaces(id) ON DELETE CASCADE;


--
-- Name: workspaces workspaces_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workspaces
    ADD CONSTRAINT workspaces_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--



-- ── Reference seeds ─────────────────────────────────────────────────────────
-- Role catalog (quotas in requests/tokens; -1 = unlimited). Guests route
-- through the anonymous role's LLM channels with a per-day limit.

INSERT INTO public.user_roles
    (role_id, role_name, daily_request_limit, monthly_request_limit, daily_token_limit,
     rpm_limit, monthly_cost_limit, default_model, models, features)
VALUES
    ('regular',   '普通用户', 50,  1500,  -1, -1, -1, '', '{}', '{"chat": true}'::jsonb),
    ('pro',       '专业版',   500, 15000, -1, -1, -1, '', '{}', '{"chat": true}'::jsonb),
    ('vip',       'VIP',      -1,  -1,    -1, -1, -1, '', '{}', '{"chat": true}'::jsonb),
    ('admin',     '管理员',    -1,  -1,    -1, -1, -1, '', '{}', '{"chat": true}'::jsonb),
    ('anonymous', '匿名用户', 20,  600,   -1, -1, -1, '', '{}', '{"chat": true}'::jsonb)
ON CONFLICT (role_id) DO NOTHING;
