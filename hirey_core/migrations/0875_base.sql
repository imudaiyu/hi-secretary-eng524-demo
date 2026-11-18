CREATE SCHEMA IF NOT EXISTS core;

CREATE TABLE IF NOT EXISTS core.demo (
    id text PRIMARY KEY,
    col text,
    old_col text
);
