--liquibase formatted sql

--changeset kolodyazhny-ma:create-table-threads
create table "threads" (
    "thread_id" varchar not null,
    "subject" varchar,
    "created_at" timestamptz not null default now(),
    constraint "threads_pkey" primary key ("thread_id")
);
create index "ix_threads_created_at" on "threads" ("created_at");
--rollback drop table "threads";

--changeset kolodyazhny-ma:create-table-messages
create table "messages" (
    "message_id" varchar not null,
    "thread_id" varchar not null,
    "author_email" varchar not null,
    "body_text" text,
    "body_html" text,
    "headers_json" jsonb,
    "created_at" timestamptz not null default now(),
    constraint "messages_pkey" primary key ("message_id"),
    constraint "fk_messages_thread_id" foreign key ("thread_id")
        references "threads" ("thread_id") on delete cascade
);
create index "ix_messages_author_email" on "messages" ("author_email");
create index "ix_messages_thread_id" on "messages" ("thread_id");
create index "ix_messages_created_at" on "messages" ("created_at");
--rollback drop table "messages";

--changeset kolodyazhny-ma:add-run-id-to-messages
alter table "messages" add column "run_id" varchar(64);
create index "ix_messages_run_id" on "messages" ("run_id") where "run_id" is not null;
--rollback drop index if exists "ix_messages_run_id"; alter table "messages" drop column "run_id";

--changeset kolodyazhny-ma:create-table-outbox-tasks
create table "outbox_tasks" (
    "id" varchar not null,
    "task_type" varchar not null,
    "email_id" varchar,
    "payload_json" jsonb,
    "task_key" varchar not null,
    "status" varchar not null default 'NEW',
    "attempt" integer not null default 0,
    "next_retry_at" timestamptz,
    "claim_token" varchar,
    "claim_until" timestamptz,
    "created_at" timestamptz not null default now(),
    "updated_at" timestamptz not null default now(),
    constraint "outbox_tasks_pkey" primary key ("id"),
    constraint "uq_outbox_task_key" unique ("task_key"),
    constraint "fk_outbox_tasks_email_id" foreign key ("email_id")
        references "messages" ("message_id") on delete set null
);
create index "ix_outbox_tasks_task_type" on "outbox_tasks" ("task_type");
create index "ix_outbox_tasks_email_id" on "outbox_tasks" ("email_id");
create index "ix_outbox_tasks_status" on "outbox_tasks" ("status");
create index "ix_outbox_tasks_created_at" on "outbox_tasks" ("created_at");
create index "ix_outbox_status_retry" on "outbox_tasks" ("status", "next_retry_at");
--rollback drop table "outbox_tasks";

--changeset kolodyazhny-ma:add-run-id-to-outbox-tasks
alter table "outbox_tasks" add column "run_id" varchar(64);
create index "ix_outbox_tasks_run_id" on "outbox_tasks" ("run_id") where "run_id" is not null;
--rollback drop index if exists "ix_outbox_tasks_run_id"; alter table "outbox_tasks" drop column "run_id";

--changeset kolodyazhny-ma:create-table-approve-deal-snapshots
create table "approve_deal_snapshots" (
    "calc_id"              varchar      not null,
    "calc_dttm"            timestamptz  not null default now(),
    "policy_rate"          numeric,
    "ets"                  numeric,
    "for_rate"             numeric,
    "eva_target"           numeric,
    "eva_indicative"       numeric,
    "asv"                  numeric,
    "option_price"         numeric,
    "crl"                  numeric,
    "cfc_so"               numeric,
    "cfc_ets"              numeric,
    "cfc_for_rate"         numeric,
    "cfc_eva_target"       numeric,
    "cfc_eva_indicative"   numeric,
    "cfc_asv"              numeric,
    "cfc_option_price"     numeric,
    "cfc_k_liq_fund"       numeric,
    constraint "approve_deal_snapshots_pkey" primary key ("calc_id")
);
--rollback drop table "approve_deal_snapshots";

--changeset kolodyazhny-ma:create-table-approve-tasks
create table "approve_tasks" (
    "task_id"          varchar      not null,
    "calc_id"          varchar      not null,
    "task_dttm"        timestamptz  not null default now(),
    "task_status"      varchar      not null,
    "agent_answer"     text,
    "deal_status"      varchar,
    constraint "approve_tasks_pkey" primary key ("task_id"),
    constraint "fk_at_calc_id" foreign key ("calc_id")
        references "approve_deal_snapshots" ("calc_id") on delete cascade
);
create index "ix_at_calc_id" on "approve_tasks" ("calc_id");
create index "ix_at_task_status" on "approve_tasks" ("task_status");
--rollback drop table "approve_tasks";

--changeset kolodyazhny-ma:invert-approve-tables-dependency
drop table "approve_tasks";
drop table "approve_deal_snapshots";

create table "approve_tasks" (
    "task_id"          varchar      not null,
    "calculation_id"   varchar      not null,
    "task_dttm"        timestamptz  not null default now(),
    "updated_at"       timestamptz  not null default now(),
    "task_status"      varchar      not null,
    "attempt_count"    integer      not null default 0,
    "agent_answer"     text,
    "deal_status"      varchar,
    "error_message"    text,
    constraint "approve_tasks_pkey" primary key ("task_id"),
    constraint "uq_at_calculation_id" unique ("calculation_id")
);
create index "ix_at_task_status" on "approve_tasks" ("task_status");
create index "ix_at_updated_at" on "approve_tasks" ("updated_at");

create table "approve_deal_snapshots" (
    "calc_id"              varchar      not null,
    "task_id"              varchar      not null,
    "calc_dttm"            timestamptz  not null default now(),
    "policy_rate"          numeric,
    "ets"                  numeric,
    "for_rate"             numeric,
    "eva_target"           numeric,
    "eva_indicative"       numeric,
    "asv"                  numeric,
    "option_price"         numeric,
    "crl"                  numeric,
    "cfc_so"               numeric,
    "cfc_ets"              numeric,
    "cfc_for_rate"         numeric,
    "cfc_eva_target"       numeric,
    "cfc_eva_indicative"   numeric,
    "cfc_asv"              numeric,
    "cfc_option_price"     numeric,
    "cfc_k_liq_fund"       numeric,
    constraint "approve_deal_snapshots_pkey" primary key ("calc_id"),
    constraint "uq_ads_task_id" unique ("task_id"),
    constraint "fk_ads_task_id" foreign key ("task_id")
        references "approve_tasks" ("task_id") on delete cascade
);
create index "ix_ads_task_id" on "approve_deal_snapshots" ("task_id");
--rollback drop table "approve_deal_snapshots"; drop table "approve_tasks"; create table "approve_deal_snapshots" ("calc_id" varchar not null, "calc_dttm" timestamptz not null default now(), "policy_rate" numeric, "ets" numeric, "for_rate" numeric, "eva_target" numeric, "eva_indicative" numeric, "asv" numeric, "option_price" numeric, "crl" numeric, "cfc_so" numeric, "cfc_ets" numeric, "cfc_for_rate" numeric, "cfc_eva_target" numeric, "cfc_eva_indicative" numeric, "cfc_asv" numeric, "cfc_option_price" numeric, "cfc_k_liq_fund" numeric, constraint "approve_deal_snapshots_pkey" primary key ("calc_id")); create table "approve_tasks" ("task_id" varchar not null, "calc_id" varchar not null, "task_dttm" timestamptz not null default now(), "task_status" varchar not null, "agent_answer" text, "deal_status" varchar, constraint "approve_tasks_pkey" primary key ("task_id"), constraint "fk_at_calc_id" foreign key ("calc_id") references "approve_deal_snapshots" ("calc_id") on delete cascade); create index "ix_at_calc_id" on "approve_tasks" ("calc_id"); create index "ix_at_task_status" on "approve_tasks" ("task_status");

--changeset kolodyazhny-ma:add-run-id-to-approve-tasks
alter table "approve_tasks" add column "run_id" varchar(64);
create index "ix_at_run_id" on "approve_tasks" ("run_id") where "run_id" is not null;
--rollback drop index if exists "ix_at_run_id"; alter table "approve_tasks" drop column "run_id";

--changeset kolodyazhny-ma:add-deal-condition-columns-to-approve-snapshots
alter table "approve_deal_snapshots"
    add column "inn"          varchar,
    add column "term_days"    integer,
    add column "volume"       double precision,
    add column "currency"     varchar,
    add column "rate"         double precision,
    add column "rate_type"    varchar,
    add column "product"      varchar,
    add column "basis"        varchar,
    add column "optionality"  varchar;
--rollback alter table "approve_deal_snapshots"
--rollback     drop column "inn",
--rollback     drop column "term_days",
--rollback     drop column "volume",
--rollback     drop column "currency",
--rollback     drop column "rate",
--rollback     drop column "rate_type",
--rollback     drop column "product",
--rollback     drop column "basis",
--rollback     drop column "optionality";

--changeset kolodyazhny-ma:add-run-id-to-approve-deal-snapshots
alter table "approve_deal_snapshots" add column "run_id" varchar(64);
create index "ix_ads_run_id" on "approve_deal_snapshots" ("run_id") where "run_id" is not null;
--rollback drop index if exists "ix_ads_run_id"; alter table "approve_deal_snapshots" drop column "run_id";

--changeset kolodyazhny-ma:create-table-deal-parsing-log
create table "deal_parsing_log" (
    "id"                 varchar      not null,
    "message_id"         varchar      not null,
    "fragment_idx"       integer      not null,
    "fragment_text"      text         not null,
    "extraction_status"  varchar      not null default 'empty',
    "intent"             varchar,
    "deal_number"        integer,
    "parsed_conditions"  jsonb,
    "error_details"      text,
    "warnings"           jsonb,
    "created_at"         timestamptz  not null default now(),
    constraint "deal_parsing_log_pkey" primary key ("id"),
    constraint "fk_dpl_message_id" foreign key ("message_id")
        references "messages" ("message_id") on delete cascade
);
create unique index "uq_dpl_message_fragment" on "deal_parsing_log" ("message_id", "fragment_idx");
create index "ix_dpl_message_id" on "deal_parsing_log" ("message_id");
create index "ix_dpl_extraction_status" on "deal_parsing_log" ("extraction_status");
--rollback drop table "deal_parsing_log";

--changeset kolodyazhny-ma:create-table-deal-snapshots
create table "deal_snapshots" (
    "id"                   varchar      not null,
    "thread_id"            varchar      not null,
    "deal_number"          integer      not null,
    "iteration"            integer      not null,
    "incoming_conditions"  jsonb,
    "request_rate"         numeric,
    "limit_rate"           numeric      not null,
    "avg_hist_rate"        numeric,
    "model_rate"           numeric,
    "policy_rate"          numeric      not null,
    "final_rate"           numeric      not null,
    "proposed_conditions"  jsonb,
    "status"               varchar      not null,
    "escalation_reason"    text,
    "parsing_log_id"       varchar      not null,
    "created_at"           timestamptz  not null default now(),
    "utilizes_limit"       bool         not null,
    "agent_text"           text         not null,
    constraint "deal_snapshots_pkey" primary key ("id"),
    constraint "fk_ds_thread_id" foreign key ("thread_id")
        references "threads" ("thread_id") on delete cascade,
    constraint "fk_ds_parsing_log_id" foreign key ("parsing_log_id")
        references "deal_parsing_log" ("id") on delete cascade
);
create unique index "uq_ds_thread_deal_iteration" on "deal_snapshots" ("thread_id", "deal_number", "iteration");
create index "ix_ds_thread_id" on "deal_snapshots" ("thread_id");
create index "ix_ds_deal_number" on "deal_snapshots" ("thread_id", "deal_number");
create index "ix_ds_parsing_log_id" on "deal_snapshots" ("parsing_log_id");
create index "ix_ds_status" on "deal_snapshots" ("status");
--rollback drop table "deal_snapshots";

--changeset kolodyazhny-ma:create-table-deal-parsing-log-fix
drop index if exists "uq_dpl_message_fragment";
alter table "deal_parsing_log" add constraint "uq_dpl_message_fragment" unique ("message_id", "fragment_idx");
--rollback alter table "deal_parsing_log" drop constraint "uq_dpl_message_fragment"; create unique index "uq_dpl_message_fragment" on "deal_parsing_log" ("message_id", "fragment_idx");

--changeset kolodyazhny-ma:add-run-id-to-deal-parsing-log
alter table "deal_parsing_log" add column "run_id" varchar(64);
--rollback alter table "deal_parsing_log" drop column "run_id";

--changeset kolodyazhny-ma:add-run-id-index-deal-parsing-log
create index "ix_dpl_run_id" on "deal_parsing_log" ("run_id") where "run_id" is not null;
--rollback drop index if exists "ix_dpl_run_id";

--changeset kolodyazhny-ma:create-table-deal-snapshots-fix
drop index if exists "uq_ds_thread_deal_iteration";
alter table "deal_snapshots" add constraint "uq_ds_parsing_log_id" unique ("parsing_log_id");
--rollback alter table "deal_snapshots" drop constraint "uq_ds_parsing_log_id"; create unique index "uq_ds_thread_deal_iteration" on "deal_snapshots" ("thread_id", "deal_number", "iteration");

--changeset kolodyazhny-ma:add-run-id-to-deal-snapshots
alter table "deal_snapshots" add column "run_id" varchar(64);
--rollback alter table "deal_snapshots" drop column "run_id";

--changeset kolodyazhny-ma:add-run-id-index-deal-snapshots
create index "ix_ds_run_id" on "deal_snapshots" ("run_id") where "run_id" is not null;
--rollback drop index if exists "ix_ds_run_id";

--changeset kolodyazhny-ma:create-table-consultant-agent-logs
create table "consultant_agent_logs" (
    "id"             varchar      not null,
    "question_dttm"  timestamptz  not null default now(),
    "session_id"     varchar      not null,
    "message_id"     varchar      not null,
    "user_id"        varchar      not null,
    "question"       text         not null,
    "agent_branch"   varchar,
    "agent_answer"   text,
    "answer_dttm"    timestamptz,
    "source_system"  varchar      not null default 'support',
    constraint "consultant_agent_logs_pkey" primary key ("id"),
    constraint "uq_cal_message_id" unique ("message_id")
);
create index "ix_cal_session_id" on "consultant_agent_logs" ("session_id");
create index "ix_cal_user_id" on "consultant_agent_logs" ("user_id");
create index "ix_cal_question_dttm" on "consultant_agent_logs" ("question_dttm");
create index "ix_cal_source_system" on "consultant_agent_logs" ("source_system");
create index "ix_cal_agent_branch" on "consultant_agent_logs" ("agent_branch");
--rollback drop table "consultant_agent_logs";

--changeset kolodyazhny-ma:add-run-id-to-consultant-agent-logs
alter table "consultant_agent_logs" add column "run_id" varchar(64);
--rollback alter table "consultant_agent_logs" drop column "run_id";
