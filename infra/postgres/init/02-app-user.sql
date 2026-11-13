-- Runs once, when the postgres volume is created (docker-entrypoint-initdb.d); also in the
-- testcontainers Postgres. Local only.
--
-- Least privilege (docs/security.md §4): the app connects as `hub_api`, a member of the group
-- role `hub_app`; migrations connect as `hub`, the owner of the schema. The privileges of
-- `hub_app` come from Alembic migration 0001, which also creates `hub_app` when it is missing
-- (on a managed database the deploy setup creates the login user, docs/operations.md §2).
-- hub_api:hub_api is a local default only, never a real credential.
CREATE ROLE hub_app NOLOGIN;
CREATE ROLE hub_api LOGIN PASSWORD 'hub_api' IN ROLE hub_app;
