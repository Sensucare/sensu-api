# Sensu API — Deployment Guide

## Prerequisites

- Docker and Docker Compose installed on the server
- PostgreSQL database running (sensu_pay database exists)
- LocTube/EVMars credentials
- Cloudinary account
- AWS credentials with SQS access

---

## 1. Clone and configure

```bash
git clone <repo-url> sensu-api
cd sensu-api
cp .env.example .env
nano .env   # fill in all values — see .env.example for descriptions
```

---

## 2. Build the Docker image

```bash
docker build -t sensu-api:local .
```

---

## 3. Docker Compose snippet

Add this service to `/opt/sensu/docker-compose.yml`:

```yaml
sensu-api:
  image: sensu-api:local
  container_name: sensu-api
  restart: unless-stopped
  env_file: /opt/sensu/sensu-api.env
  ports:
    - "8001:8001"
  depends_on:
    - sensu-postgres
  volumes:
    - sensu-api-static:/app/static
```

The env file path (`/opt/sensu/sensu-api.env`) must contain all variables from `.env.example` with real values.

---

## 4. Database

The API uses an existing PostgreSQL database. No migrations are run by the API itself — Prisma migrations are managed by sensu-pay.

Verify connectivity after start:

```bash
curl http://localhost:8001/api/health
# Expected: {"status":"ok","database":"connected"}
```

---

## 5. Start / restart

```bash
# First run
docker compose up -d sensu-api

# After rebuilding image
docker build -t sensu-api:local .
docker restart sensu-api

# View logs
docker logs -f sensu-api
```

---

## 6. Verify MQTT is running

```bash
curl http://localhost:8001/api/mqtt/status
# Expected: {"running":true, ...}
```

If `running` is false, check that all `EVIEW_MQTT_*` variables are correct and the LocTube broker is reachable.

---

## 7. Nginx proxy (already configured on production)

Traffic is routed via Nginx on the same host:

```
https://api.sensu.com.mx/api/  →  http://sensu-api:8001/api/
```

No changes needed unless the port changes.

---

## 8. Static files (avatars)

The container serves profile images from `/app/static/avatars/`. Mount a persistent volume so avatars survive container restarts (see step 3 above).

---

## Troubleshooting

| Symptom | Check |
|---|---|
| `database: disconnected` on /api/health | DATABASE_URL is wrong or postgres container not running |
| `mqtt_running: false` | EVIEW_MQTT_* credentials wrong or broker unreachable |
| 401 on all endpoints | JWT_SECRET_KEY mismatch between api and pay containers |
| Avatars disappear after restart | Static volume not mounted |
