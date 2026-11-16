-- 1. Тестовые профили пользователей
CREATE TABLE IF NOT EXISTS users (
    user_id VARCHAR(64) PRIMARY KEY,
    login VARCHAR(64) UNIQUE NOT NULL,
    team VARCHAR(64) NOT NULL,
    role VARCHAR(32) NOT NULL DEFAULT 'intern'
);

-- 2. чеклисты стажёров
CREATE TABLE IF NOT EXISTS tasks (
    task_id VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    environment VARCHAR(64), -- NULL для проверки сценария с неизвестным стендом
    link TEXT,
    version VARCHAR(64),
    expected_result TEXT
);

-- 3. Заявки на доступ
CREATE TABLE IF NOT EXISTS access_requests (
    id SERIAL PRIMARY KEY,
    request_number VARCHAR(32) UNIQUE NOT NULL,
    user_id VARCHAR(64) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    resource_id VARCHAR(64) NOT NULL,
    access_level VARCHAR(32) NOT NULL DEFAULT 'read',
    reason TEXT NOT NULL,
    status VARCHAR(32) NOT NULL CHECK (status IN ('pending', 'granted')),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    -- Защита от дублей при повторных кликах и сетевых сбоях
    CONSTRAINT uq_user_resource_access UNIQUE (user_id, resource_id, access_level)
);

-- 4. права доступа
CREATE TABLE IF NOT EXISTS user_access (
    user_id VARCHAR(64) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    resource_id VARCHAR(64) NOT NULL,
    access_level VARCHAR(32) NOT NULL DEFAULT 'read',
    status VARCHAR(32) NOT NULL CHECK (status IN ('missing', 'granted')),
    PRIMARY KEY (user_id, resource_id, access_level)
);

-- 5. сессии чата
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    pending_clarification JSONB,
    messages JSONB NOT NULL DEFAULT '[]'::jsonb,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);