PRAGMA user_version = 16;

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE providers (
    id TEXT NOT NULL,
    app_type TEXT NOT NULL,
    name TEXT NOT NULL,
    settings_config TEXT NOT NULL,
    website_url TEXT,
    category TEXT,
    created_at INTEGER,
    sort_index INTEGER,
    notes TEXT,
    icon TEXT,
    icon_color TEXT,
    meta TEXT NOT NULL DEFAULT '{}',
    is_current BOOLEAN NOT NULL DEFAULT 0,
    in_failover_queue BOOLEAN NOT NULL DEFAULT 0,
    cost_multiplier TEXT NOT NULL DEFAULT '1.0',
    limit_daily_usd TEXT,
    limit_monthly_usd TEXT,
    provider_type TEXT,
    PRIMARY KEY (id, app_type)
);

INSERT INTO settings (key, value) VALUES
    ('common_config_codex', '[plugins.chrome]\nenabled = true\n');

INSERT INTO providers (id, app_type, name, settings_config, is_current, provider_type) VALUES
    ('provider-proxy', 'codex', 'Fixture Proxy', '{"config":"model_provider = \\"custom\\""}', 1, 'custom');
