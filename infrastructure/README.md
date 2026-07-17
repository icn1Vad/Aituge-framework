# Aituge appliance data services

This directory owns shared infrastructure for the appliance. Application code
stays in sibling projects and connects over Docker networking or the local
development ports.

## Services

- PostgreSQL 16 with pgvector 0.8.5
- Redis 7.2 with AOF persistence

The initial PostgreSQL volume creates two isolated databases:

- `aituge`: framework runtime data
- `proofreading`: proofreading domain data

pgvector is enabled in `aituge` and `proofreading`.

## Local commands

```bash
docker compose --env-file .env -f compose.data.yml pull
docker compose --env-file .env -f compose.data.yml up -d
docker compose --env-file .env -f compose.data.yml ps
```

Verify the services:

```bash
docker compose --env-file .env -f compose.data.yml exec postgres \
  psql -U appliance -d proofreading -c "SELECT extversion FROM pg_extension WHERE extname = 'vector';"

docker compose --env-file .env -f compose.data.yml exec redis \
  redis-cli -a aituge-local-redis --no-auth-warning ping
```

The committed `.env.example` contains placeholders. The ignored `.env` is only
for local development; replace its credentials before an appliance deployment.
