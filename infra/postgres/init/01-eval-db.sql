-- Runs once, when the postgres volume is created (docker-entrypoint-initdb.d).
-- Evaluation runs use their own database (EVAL_DATABASE_URL), so they never mix with demo data.
-- Database roles: see 02-app-user.sql.
CREATE DATABASE hub_eval;
