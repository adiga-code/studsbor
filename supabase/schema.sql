-- Схема базы «Фонда студсовета» для Supabase.
-- Нужна, только если /api/setup не смог применить миграции сам:
-- Supabase → SQL Editor → вставить файл целиком → Run.
-- Сгенерировано: alembic upgrade head --sql

BEGIN;

CREATE TABLE alembic_version (
    version_num VARCHAR(32) NOT NULL, 
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

-- Running upgrade  -> c12e6af18580

CREATE TABLE users (
    id BIGSERIAL NOT NULL, 
    telegram_id BIGINT NOT NULL, 
    username VARCHAR(64), 
    first_name VARCHAR(128), 
    display_name VARCHAR(64), 
    is_anonymous BOOLEAN NOT NULL, 
    reminders_enabled BOOLEAN NOT NULL, 
    reminder_period_months INTEGER NOT NULL, 
    is_blocked BOOLEAN NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    CONSTRAINT pk_users PRIMARY KEY (id)
);

CREATE UNIQUE INDEX ix_users_telegram_id ON users (telegram_id);

CREATE TABLE admins (
    user_id BIGINT NOT NULL, 
    role VARCHAR(16) NOT NULL, 
    granted_by BIGINT, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    CONSTRAINT pk_admins PRIMARY KEY (user_id), 
    CONSTRAINT fk_admins_granted_by_users FOREIGN KEY(granted_by) REFERENCES users (id), 
    CONSTRAINT fk_admins_user_id_users FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE TABLE fundraisers (
    id BIGSERIAL NOT NULL, 
    title VARCHAR(128) NOT NULL, 
    description TEXT NOT NULL, 
    goal_amount BIGINT, 
    status VARCHAR(16) NOT NULL, 
    is_general BOOLEAN NOT NULL, 
    starts_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    ends_at TIMESTAMP WITH TIME ZONE, 
    created_by BIGINT, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    CONSTRAINT pk_fundraisers PRIMARY KEY (id), 
    CONSTRAINT ck_fundraisers_goal_positive CHECK (goal_amount IS NULL OR goal_amount > 0), 
    CONSTRAINT fk_fundraisers_created_by_users FOREIGN KEY(created_by) REFERENCES users (id)
);

CREATE TABLE notification_logs (
    id BIGSERIAL NOT NULL, 
    user_id BIGINT NOT NULL, 
    notification_type VARCHAR(32) NOT NULL, 
    sent_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    status VARCHAR(16) NOT NULL, 
    error TEXT, 
    CONSTRAINT pk_notification_logs PRIMARY KEY (id), 
    CONSTRAINT fk_notification_logs_user_id_users FOREIGN KEY(user_id) REFERENCES users (id)
);

CREATE INDEX ix_notification_logs_user_type ON notification_logs (user_id, notification_type);

CREATE TABLE expenses (
    id BIGSERIAL NOT NULL, 
    fundraiser_id BIGINT NOT NULL, 
    amount BIGINT NOT NULL, 
    description TEXT NOT NULL, 
    receipt_file_id VARCHAR(255), 
    receipt_file_type VARCHAR(16), 
    receipt_url TEXT, 
    created_by BIGINT NOT NULL, 
    spent_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    CONSTRAINT pk_expenses PRIMARY KEY (id), 
    CONSTRAINT ck_expenses_amount_positive CHECK (amount > 0), 
    CONSTRAINT fk_expenses_created_by_users FOREIGN KEY(created_by) REFERENCES users (id), 
    CONSTRAINT fk_expenses_fundraiser_id_fundraisers FOREIGN KEY(fundraiser_id) REFERENCES fundraisers (id)
);

CREATE INDEX ix_expenses_fundraiser_id ON expenses (fundraiser_id);

CREATE TABLE payments (
    id BIGSERIAL NOT NULL, 
    user_id BIGINT NOT NULL, 
    fundraiser_id BIGINT NOT NULL, 
    amount BIGINT NOT NULL, 
    currency VARCHAR(3) NOT NULL, 
    invoice_payload VARCHAR(64) NOT NULL, 
    telegram_payment_charge_id VARCHAR(128), 
    provider_payment_charge_id VARCHAR(128), 
    status VARCHAR(32) NOT NULL, 
    description VARCHAR(255) NOT NULL, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    paid_at TIMESTAMP WITH TIME ZONE, 
    CONSTRAINT pk_payments PRIMARY KEY (id), 
    CONSTRAINT ck_payments_amount_positive CHECK (amount > 0), 
    CONSTRAINT fk_payments_fundraiser_id_fundraisers FOREIGN KEY(fundraiser_id) REFERENCES fundraisers (id), 
    CONSTRAINT fk_payments_user_id_users FOREIGN KEY(user_id) REFERENCES users (id), 
    CONSTRAINT uq_payments_invoice_payload UNIQUE (invoice_payload), 
    CONSTRAINT uq_payments_telegram_payment_charge_id UNIQUE (telegram_payment_charge_id)
);

CREATE INDEX ix_payments_fundraiser_id ON payments (fundraiser_id);

CREATE INDEX ix_payments_status_paid_at ON payments (status, paid_at);

CREATE INDEX ix_payments_user_id ON payments (user_id);

CREATE TABLE payment_events (
    id BIGSERIAL NOT NULL, 
    event_key VARCHAR(160) NOT NULL, 
    event_type VARCHAR(64) NOT NULL, 
    payment_id BIGINT, 
    payload TEXT, 
    processed_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    CONSTRAINT pk_payment_events PRIMARY KEY (id), 
    CONSTRAINT fk_payment_events_payment_id_payments FOREIGN KEY(payment_id) REFERENCES payments (id), 
    CONSTRAINT uq_payment_events_event_key UNIQUE (event_key)
);

CREATE INDEX ix_payment_events_payment_id ON payment_events (payment_id);

CREATE TABLE refunds (
    id BIGSERIAL NOT NULL, 
    payment_id BIGINT NOT NULL, 
    amount BIGINT NOT NULL, 
    status VARCHAR(16) NOT NULL, 
    reason VARCHAR(255), 
    created_by BIGINT, 
    created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    succeeded_at TIMESTAMP WITH TIME ZONE, 
    CONSTRAINT pk_refunds PRIMARY KEY (id), 
    CONSTRAINT ck_refunds_amount_positive CHECK (amount > 0), 
    CONSTRAINT fk_refunds_created_by_users FOREIGN KEY(created_by) REFERENCES users (id), 
    CONSTRAINT fk_refunds_payment_id_payments FOREIGN KEY(payment_id) REFERENCES payments (id)
);

CREATE INDEX ix_refunds_payment_id ON refunds (payment_id);

INSERT INTO alembic_version (version_num) VALUES ('c12e6af18580') RETURNING alembic_version.version_num;

-- Running upgrade c12e6af18580 -> a2250d07e5f0

CREATE TABLE fsm_states (
    key VARCHAR(255) NOT NULL, 
    state VARCHAR(255), 
    data TEXT NOT NULL, 
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
    CONSTRAINT pk_fsm_states PRIMARY KEY (key)
);

ALTER TABLE users ENABLE ROW LEVEL SECURITY;

ALTER TABLE fundraisers ENABLE ROW LEVEL SECURITY;

ALTER TABLE payments ENABLE ROW LEVEL SECURITY;

ALTER TABLE refunds ENABLE ROW LEVEL SECURITY;

ALTER TABLE expenses ENABLE ROW LEVEL SECURITY;

ALTER TABLE admins ENABLE ROW LEVEL SECURITY;

ALTER TABLE notification_logs ENABLE ROW LEVEL SECURITY;

ALTER TABLE payment_events ENABLE ROW LEVEL SECURITY;

ALTER TABLE fsm_states ENABLE ROW LEVEL SECURITY;

ALTER TABLE alembic_version ENABLE ROW LEVEL SECURITY;

UPDATE alembic_version SET version_num='a2250d07e5f0' WHERE alembic_version.version_num = 'c12e6af18580';

COMMIT;

