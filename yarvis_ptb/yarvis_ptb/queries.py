INIT_VECTOR = """
CREATE EXTENSION IF NOT EXISTS vector;
"""

INIT_INVOCATIONS_QUERY = """
CREATE TABLE IF NOT EXISTS invocations (
   id SERIAL PRIMARY KEY,
   created_at TIMESTAMPTZ NOT NULL,
   scheduled_at TIMESTAMPTZ NOT NULL,
   chat_id BIGINT NOT NULL,
   embedding vector(384),
   is_active BOOLEAN NOT NULL DEFAULT true,
   is_recurring BOOLEAN NOT NULL DEFAULT false,
   reason TEXT NOT NULL,
   meta JSONB
);

CREATE INDEX IF NOT EXISTS idx_invocations_chat_active ON invocations (chat_id, is_active);


CREATE INDEX  IF NOT EXISTS messages_embedding_idx ON messages
USING ivfflat (embedding vector_ip_ops)  -- or vector_cosine_ops
WITH (lists = 100);
"""

INIT_SCHEDULES_QUERY = """
CREATE TABLE IF NOT EXISTS schedules (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL,
    next_run_at TIMESTAMPTZ NOT NULL,
    chat_id BIGINT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    reason TEXT NOT NULL,
    context TEXT,
    schedule_type VARCHAR(20) NOT NULL,
    schedule_spec TEXT,
    meta JSONB
);

CREATE INDEX IF NOT EXISTS idx_schedules_active ON schedules (is_active) WHERE is_active = true;
"""

MIGRATE_INVOCATIONS_TO_SCHEDULES = """
INSERT INTO schedules (created_at, next_run_at, chat_id, is_active, reason, schedule_type, schedule_spec, meta)
SELECT created_at, scheduled_at, chat_id, is_active, reason,
       CASE WHEN is_recurring THEN 'every' ELSE 'at' END,
       CASE WHEN is_recurring THEN '1d' ELSE NULL END,
       meta
FROM invocations
WHERE is_active = true
AND NOT EXISTS (
    SELECT 1 FROM schedules
    WHERE schedules.chat_id = invocations.chat_id
    AND schedules.reason = invocations.reason
    AND schedules.is_active = true
);
"""


INIT_VARIABLES_QUERY = """
CREATE TABLE  IF NOT EXISTS  chat_variables(
    chat_id INTEGER,
    name VARCHAR(255) NOT NULL,
    value TEXT NULL,
    datatype VARCHAR(50) NOT NULL
);


CREATE INDEX IF NOT EXISTS idx_chat_variables_chat_name ON chat_variables (chat_id, name);

"""

INIT_MEMORY_QUERY = """
CREATE TABLE  IF NOT EXISTS  memories (
    id SERIAL PRIMARY KEY,
    mem_id VARCHAR(255) NOT NULL,
    chat_id BIGINT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    content TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT true,
    extra JSONB
);

-- Create a composite index for efficient filtering of active messages by chat_id
CREATE INDEX IF NOT EXISTS  idx_memories_chat_active ON memories (chat_id, active)
WHERE active = true;
"""


INIT_MESSAGES_QUERY = """
CREATE TABLE IF NOT EXISTS messages (
    -- Using timestamptz to store timestamp with timezone information
    created_at TIMESTAMPTZ NOT NULL,
    chat_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    is_visible BOOLEAN DEFAULT true NOT NULL,
    message TEXT NOT NULL,  -- Using TEXT for unlimited length string
    marked_for_archive BOOLEAN NOT NULL DEFAULT false,
    meta JSONB,            -- Using JSONB for better performance and indexing capabilities

    -- Adding a primary key for better table organization
    id SERIAL PRIMARY KEY
);

-- Create an index for efficient retrieval of messages by chat_id and timestamp
-- This compound index will be used when querying last N messages in a chat
CREATE INDEX IF NOT EXISTS idx_messages_chat_timestamp
ON messages (chat_id, created_at DESC);
"""

INIT_AGENTS_QUERY = """
CREATE TABLE IF NOT EXISTS agents (
    id SERIAL PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    meta JSONB
);
"""

MIGRATE_MESSAGES_AGENT_ID = """
ALTER TABLE messages ADD COLUMN IF NOT EXISTS agent_id INTEGER DEFAULT NULL;
"""
