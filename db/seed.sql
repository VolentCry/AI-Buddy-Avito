INSERT INTO users (user_id, login, team, role) VALUES
    ('intern-catalog', 'Стажёр Каталога (без доступа)', 'catalog', 'intern'),
    ('intern-catalog-pending', 'Стажёр Каталога (заявка на согласовании)', 'catalog', 'intern'),
    ('intern-catalog-granted', 'Стажёр Каталога (доступ выдан)', 'catalog', 'intern'),
    ('intern-catalog-no-env', 'Стажёр Каталога (стенд не указан)', 'catalog', 'intern'),
    ('intern-other', 'Стажёр другой команды', 'platform', 'intern')
ON CONFLICT (user_id) DO NOTHING;

INSERT INTO tasks (task_id, user_id, environment, link, version, expected_result) VALUES
    ('task-1', 'intern-catalog', 'catalog-dev', 'https://catalog-dev.avito.local/items/42', 'v1.4.2-fix', 'Карточка товара открывается без ошибки 500'),
    ('task-2', 'intern-catalog-pending', 'catalog-dev', 'https://catalog-dev.avito.local/items/42', 'v1.4.2-fix', 'Карточка товара открывается без ошибки 500'),
    ('task-3', 'intern-catalog-granted', 'catalog-dev', 'https://catalog-dev.avito.local/items/42', 'v1.4.2-fix', 'Карточка товара открывается без ошибки 500'),
    ('task-4', 'intern-catalog-no-env', NULL, 'https://tracker.avito.local/TASK-104', NULL, 'Проверить работу нового фильтра'),
    ('task-5', 'intern-other', 'platform-dev', 'https://platform-dev.avito.local/health', 'v2.0.1', 'Эндпоинт возвращает 200 OK')
ON CONFLICT (task_id) DO NOTHING;

INSERT INTO access_requests (request_number, user_id, resource_id, access_level, reason, status) VALUES
    ('ACC-014', 'intern-catalog-pending', 'catalog-dev', 'read', 'Проверка исправления по первой задаче', 'pending'),
    ('ACC-015', 'intern-catalog-granted', 'catalog-dev', 'read', 'Проверка исправления по первой задаче', 'granted')
ON CONFLICT (user_id, resource_id, access_level) DO NOTHING;

INSERT INTO user_access (user_id, resource_id, access_level, status) VALUES
    ('intern-catalog', 'catalog-dev', 'read', 'missing'),
    ('intern-catalog-pending', 'catalog-dev', 'read', 'missing'),
    ('intern-catalog-granted', 'catalog-dev', 'read', 'granted'),
    ('intern-catalog-no-env', 'catalog-dev', 'read', 'missing'),
    ('intern-other', 'catalog-dev', 'read', 'missing')
ON CONFLICT (user_id, resource_id, access_level) DO NOTHING;