# ADR-0015: Free-tier deployment: Cloud Run + Neon + Qdrant Cloud

- Status: accepted
- Date: 2026-11-12

## Context
The public demo is for a portfolio. It has almost no traffic and should cost close to nothing. It should still show real deployment practice: containers, managed database, secrets, CI/CD, rollback.

## Decision
Deploy the API (with the built web app) and the MCP server as two Google Cloud Run services that scale to zero. Use Neon (Postgres), Qdrant Cloud (vector search) and Langfuse Cloud on their free plans. Deploy from GitHub Actions. Keep a working `compose.prod.yml` as a fallback for a single VPS.

## Alternatives considered
- **One VPS with Docker Compose (about €5–10 per month).** Simplest and always warm; costs money every month; the fallback option.
- **AWS ECS Fargate + RDS.** Closest to enterprise setups; about €40–60 per month and much more setup.
- **Render / Fly.io.** Free plans are limited or gone; less control.
- **Local only + video.** No public link.

## Consequences
Cold starts of a few seconds; the database sleeps after minutes without queries, and the free vector cluster is suspended after a week without use (a scheduled ping prevents this). Free plans change — limits are checked at build time. Scale-to-zero means no background work, which shaped ADR-0007. A cloud billing account with a budget alert is needed.
