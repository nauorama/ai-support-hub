-- Runs once, when the postgres volume is created (docker-entrypoint-initdb.d).
-- Evaluation runs use their own database (EVAL_DATABASE_URL), so they never mix with demo data.
-- Database roles (app, migrator) are created by the migrations in P2-02, not here.
CREATE DATABASE hub_eval;
