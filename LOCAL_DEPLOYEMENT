# Elitea Local Deployment Guide for macOS Apple Silicon
## Using VSCode, Podman Desktop, and `centry`

This guide explains how to run the full **Elitea local environment** on **macOS Apple Silicon** using:

- **VSCode**
- **Podman Desktop**
- the **`EliteaAI/centry`** repository

This guide is focused on the **full local deployment** flow and includes the required **artifact storage (MinIO) addition** for local usage.

---

## 1. Overview

The local Elitea platform is started from the **`centry`** repository.

The deployment runs these core services:

- **Redis**
- **Postgres with pgvector**
- **pylon_auth**
- **pylon_main**
- **pylon_indexer**

For local artifacts support, you should also run:

- **MinIO**

The main application is available locally at:

- `http://localhost`

By default:

- host port `80` is mapped to container port `8080`

---

## 2. What this guide covers

This guide includes:

1. prerequisites
2. Podman setup on macOS Apple Silicon
3. cloning and opening the `centry` repository
4. environment setup
5. generation of `SECRETS_MASTER_KEY`
6. starting the full local stack
7. verification commands
8. update and restart workflow
9. optional local artifact storage setup with **MinIO**
10. troubleshooting
11. daily workflow
12. command cheat sheet

---

## 3. Prerequisites

You should have these tools available.

### Required
- **Git**
- **VSCode**
- **Podman Desktop**
- **Podman CLI**

### Already available in your setup
- VSCode
- Podman Desktop
- Python 3.13

> Python on your Mac is not required for the standard local deployment path because Elitea runs inside containers.

---

## 4. Verify required tools

### 4.1 Git
Check Git:

```bash
git --version
```

If Git is missing, install it with Homebrew:

```bash
brew install git
```

---

### 4.2 VSCode
Check that VSCode opens normally.

If you want to launch VSCode from Terminal, verify:

```bash
code --version
```

If `code` is unavailable, open VSCode manually and use:

- **File → Open Folder**

---

### 4.3 Podman
Check Podman:

```bash
podman --version
```

Check Compose support:

```bash
podman compose version
```

---

## 5. Podman Desktop setup on macOS Apple Silicon

Podman on macOS runs containers inside a lightweight Linux VM called a **Podman machine**.

### 5.1 Check machine status

```bash
podman machine list
```

If needed, start it:

```bash
podman machine start
```

---

### 5.2 Verify Podman works

Run:

```bash
podman run --rm hello-world
```

If this succeeds, Podman is ready.

---

## 6. Recommended workspace folder

Create a local workspace:

```bash
mkdir -p ~/elitea
cd ~/elitea
```

Recommended structure:

```text
~/elitea/
└── centry/
```

---

## 7. Clone the repository

Clone the repository:

```bash
cd ~/elitea
git clone https://github.com/EliteaAI/centry.git
```

Enter the repository:

```bash
cd ~/elitea/centry
```

---

## 8. Open the repository in VSCode

Open the project:

```bash
code ~/elitea/centry
```

If the `code` command is unavailable, open VSCode manually and open the folder:

- `~/elitea/centry`

---

## 9. Important files and folders

Important files in the repository:

```text
centry/
├── README.md
├── docker-compose.yml
├── envs/
│   ├── default.env
│   └── override.env
├── pylon_auth/
├── pylon_main/
├── pylon_indexer/
└── ssl/
```

What matters most:

- `docker-compose.yml` → local services definition
- `envs/default.env` → base environment values
- `envs/override.env` → your local overrides
- `ssl/` → optional TLS certificates

---

## 10. Create your local environment file

Create `override.env` from the default file:

```bash
cd ~/elitea/centry
cp envs/default.env envs/override.env
```

This file is intended for local customization.

---

## 11. Base environment values

The repository provides these default values:

```dotenv
APP_HOST=
APP_PROTO=http

COOKIES_SECURE=false
COOKIES_LIFETIME=604800

ELITEA_RELEASE=main
ELITEA_UI_RELEASE=latest

DEFAULT_ADMIN_PASSWORD=admin

NAME_PREFIX=centry

APPLICATION_AUTH_SECRET_KEY=changeme
APPLICATION_MAIN_SECRET_KEY=changeme

EVENT_HMAC_KEY=events_hmac_key
RPC_HMAC_KEY=rpc_hmac_key
EXPOSURE_HMAC_KEY=exposure_hmac_key
INDEXER_HMAC_KEY=indexer_hmac_key

SECRETS_MASTER_KEY=INw4szNnkFENciD_DpAhrBxkuNdS0Ogfk7eUPUnwkPE=

REDIS_HOST=redis
REDIS_PORT=6379
REDIS_SSL=false
REDIS_PASSWORD=changeme

POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_USER=centry
POSTGRES_PASSWORD=changeme
POSTGRES_DB=db
POSTGRES_INITDB_ARGS=--data-checksums
```

---

## 12. Minimum required changes

For this local guide, you can leave most defaults as-is if you want a quick local setup.

### Required to change
You **should change**:

```dotenv
SECRETS_MASTER_KEY=<GENERATED_KEY>
```

### Recommended local values
Set:

```dotenv
APP_HOST=localhost
APP_PROTO=http
```

You confirmed that `APP_HOST` may also be:

- `localhost`
- your Mac’s Ethernet IP
- your Mac’s Wi-Fi IP

For normal local development, use:

```dotenv
APP_HOST=localhost
```

---

## 13. Generate `SECRETS_MASTER_KEY`

Run this command exactly:

```bash
podman run --rm --entrypoint= ghcr.io/eliteaai/pylon:1.2.25 \
  python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

Copy the result and place it into:

```dotenv
SECRETS_MASTER_KEY=<PASTE_GENERATED_KEY_HERE>
```

---

## 14. Recommended `envs/override.env`

For a minimal local setup, this is enough:

```dotenv
APP_HOST=localhost
APP_PROTO=http

SECRETS_MASTER_KEY=<PASTE_GENERATED_KEY_HERE>
```

If you want to be more explicit, you may include the full local file:

```dotenv
APP_HOST=localhost
APP_PROTO=http

COOKIES_SECURE=false
COOKIES_LIFETIME=604800

ELITEA_RELEASE=main
ELITEA_UI_RELEASE=latest

DEFAULT_ADMIN_PASSWORD=admin

NAME_PREFIX=centry

APPLICATION_AUTH_SECRET_KEY=changeme
APPLICATION_MAIN_SECRET_KEY=changeme

EVENT_HMAC_KEY=events_hmac_key
RPC_HMAC_KEY=rpc_hmac_key
EXPOSURE_HMAC_KEY=exposure_hmac_key
INDEXER_HMAC_KEY=indexer_hmac_key

SECRETS_MASTER_KEY=<PASTE_GENERATED_KEY_HERE>

REDIS_HOST=redis
REDIS_PORT=6379
REDIS_SSL=false
REDIS_PASSWORD=changeme

POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_USER=centry
POSTGRES_PASSWORD=changeme
POSTGRES_DB=db
POSTGRES_INITDB_ARGS=--data-checksums
```

---

## 15. Start the local platform

Go to the repository root:

```bash
cd ~/elitea/centry
```

Pull images:

```bash
podman compose pull
```

Start the stack:

```bash
podman compose up -d
```

Follow logs:

```bash
podman compose logs -f
```

Stop following logs with:

```text
Ctrl + C
```

This does **not** stop the containers.

---

## 16. What the default stack runs

The default `docker-compose.yml` starts:

| Service | Image | Purpose |
|--------|-------|---------|
| `redis` | `redis:8.2.4-alpine` | Redis queues / sessions / event transport |
| `postgres` | `pgvector/pgvector:0.8.1-pg18` | Main PostgreSQL database |
| `pylon_auth` | `ghcr.io/eliteaai/pylon:1.2.25` | authentication service |
| `pylon_main` | `ghcr.io/eliteaai/pylon:1.2.25` | main web platform |
| `pylon_indexer` | `ghcr.io/eliteaai/pylon:1.2.25` | background/indexing service |

---

## 17. Access the application

Open the local platform:

```bash
open http://localhost
```

If you set `APP_HOST` to a LAN IP instead of `localhost`, use that IP in the browser:

```text
http://<your-mac-ip>
```

---

## 18. Verify the deployment

### 18.1 Check service status

```bash
podman compose ps
```

Expected services:

- `redis`
- `postgres`
- `pylon_auth`
- `pylon_main`
- `pylon_indexer`

---

### 18.2 Check running containers

```bash
podman ps
```

---

### 18.3 Check logs

All services:

```bash
podman compose logs -f
```

Specific services:

```bash
podman compose logs -f pylon_main
```

```bash
podman compose logs -f pylon_auth
```

```bash
podman compose logs -f pylon_indexer
```

```bash
podman compose logs -f postgres
```

```bash
podman compose logs -f redis
```

---

## 19. Database and Redis volumes

In `docker-compose.yml`, this section is correct:

```yaml
volumes:
  redis-data:
  postgres-data:
```

You do **not** need to assign manual values there for normal usage.

These are **named volumes** and Podman/Compose creates them automatically.

They are used for persistent local storage of:

- Redis data
- PostgreSQL data

---

## 20. Why artifacts storage is missing by default

The default local deployment provides persistence for:

- Redis
- PostgreSQL

But local artifacts require an **S3-compatible object storage backend**.

The Artifacts subsystem expects **MinIO/S3-style storage**, and the current local deployment does not include a MinIO service by default.

That is why you may see:

- Artifacts page available
- but no buckets
- or no usable storage selection

To fix this, add **MinIO** to the local deployment.

---

# 21. Add local MinIO support for artifacts

## 21.1 Why MinIO is needed

Local artifacts such as:

- attachment buckets
- image-related buckets
- general artifact buckets

need object storage.

For local deployment, the correct approach is to run **MinIO** in the same Podman Compose stack.

---

## 21.2 Update `envs/override.env`

Add these values:

```dotenv
MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=<CHANGE_ME_MINIO_SECRET>
MINIO_REGION=us-east-1
MINIO_URL=http://minio:9000
```

Example full minimal local override file with MinIO:

```dotenv
APP_HOST=localhost
APP_PROTO=http

SECRETS_MASTER_KEY=<PASTE_GENERATED_KEY_HERE>

MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=<CHANGE_ME_MINIO_SECRET>
MINIO_REGION=us-east-1
MINIO_URL=http://minio:9000
```

---

## 21.3 Update `docker-compose.yml`

Add a new `minio` service.

### Add this service under `services:`

```yaml
  minio:
    image: minio/minio
    restart: unless-stopped
    logging:
      driver: "json-file"
      options:
        max-file: "5"
        max-size: "10m"
    env_file:
      - ./envs/default.env
      - ./envs/override.env
    command: server /data --console-address ":9001"
    volumes:
      - minio-data:/data
    ports:
      - "9000:9000"
      - "9001:9001"
    networks:
      - centry
```

---

### Add this volume under `volumes:`

```yaml
volumes:
  redis-data:
  postgres-data:
  minio-data:
```

---

## 21.4 Resulting storage volumes section

After the change, your volumes section should look like this:

```yaml
volumes:
  redis-data:
  postgres-data:
  minio-data:
```

Meaning:

- `redis-data` → Redis persistence
- `postgres-data` → PostgreSQL persistence
- `minio-data` → artifacts/object storage persistence

---

## 21.5 Restart after adding MinIO

Run:

```bash
cd ~/elitea/centry
podman compose down
podman compose up -d
```

Then verify:

```bash
podman compose ps
```

You should now also see:

- `minio`

---

## 21.6 Verify MinIO

Open MinIO console:

```bash
open http://localhost:9001
```

Log in using:

- Access Key = `MINIO_ACCESS_KEY`
- Secret Key = `MINIO_SECRET_KEY`

For example:

- username: `minioadmin`
- password: the value from `MINIO_SECRET_KEY`

---

## 21.7 Why this fixes artifacts

With MinIO enabled:

- the Artifacts subsystem has S3-compatible storage
- buckets can be created
- uploads can create buckets automatically
- local artifact storage becomes persistent through `minio-data`

---

## 22. Full example `docker-compose.yml` addition for MinIO

Add this under `services:`:

```yaml
  minio:
    image: minio/minio
    restart: unless-stopped
    logging:
      driver: "json-file"
      options:
        max-file: "5"
        max-size: "10m"
    env_file:
      - ./envs/default.env
      - ./envs/override.env
    command: server /data --console-address ":9001"
    volumes:
      - minio-data:/data
    ports:
      - "9000:9000"
      - "9001:9001"
    networks:
      - centry
```

And ensure your volumes block contains:

```yaml
volumes:
  redis-data:
  postgres-data:
  minio-data:
```

---

## 23. How to verify artifacts support works

After MinIO is added and the stack is restarted:

1. open the local platform
2. go to **Artifacts**
3. verify that storage is available
4. create a bucket or upload a file
5. confirm the bucket appears

You can also verify MinIO directly in its console:

- `http://localhost:9001`

---

## 24. Optional: use a network IP instead of localhost

If you want to access Elitea from another device on your local network:

1. find your Mac’s IP
   ```bash
   ipconfig getifaddr en0
   ```

2. set it in `envs/override.env`
   ```dotenv
   APP_HOST=<your-mac-ip>
   ```

3. restart the stack
   ```bash
   podman compose down
   podman compose up -d
   ```

4. open in browser
   ```text
   http://<your-mac-ip>
   ```

---

## 25. Update later

To update the repository and container images:

```bash
cd ~/elitea/centry
git pull
podman compose pull
podman compose up -d
```

Then inspect logs:

```bash
podman compose logs -f
```

---

## 26. Stop the environment

Stop all containers:

```bash
cd ~/elitea/centry
podman compose down
```

This keeps the volumes.

---

## 27. Reset the environment completely

If you want a fully clean local reset:

```bash
cd ~/elitea/centry
podman compose down -v
podman compose pull
podman compose up -d
```

This removes:

- Redis data
- PostgreSQL data
- MinIO data

Use this only if you intentionally want a fresh start.

---

## 28. Optional SSL setup

If you want HTTPS locally:

1. place certificates in:

```text
ssl/
├── server.crt
├── server.key
└── ca.crt
```

2. uncomment SSL-related options in:
- `pylon_*/pylon.yml`
- `docker-compose.yml`

3. set:

```dotenv
APP_PROTO=https
```

If you do not need HTTPS locally, keep:

```dotenv
APP_PROTO=http
```

---

## 29. Troubleshooting

### Podman machine not running
Check:

```bash
podman machine list
```

Start it:

```bash
podman machine start
```

---

### `podman compose` fails
Check:

```bash
podman compose version
```

If it fails, verify Podman Desktop installation.

---

### Port 80 is already in use
Check:

```bash
lsof -i :80
```

If needed, change this mapping in `docker-compose.yml`:

```yaml
ports:
  - "80:8080"
```

to:

```yaml
ports:
  - "8080:8080"
```

Then access:

```text
http://localhost:8080
```

---

### MinIO console not available
Check service status:

```bash
podman compose ps
```

Check MinIO logs:

```bash
podman compose logs -f minio
```

---

### Artifacts page still empty
Check:

1. MinIO service is running
2. `MINIO_ACCESS_KEY` is set
3. `MINIO_SECRET_KEY` is set
4. `MINIO_URL=http://minio:9000`
5. stack was restarted after env and compose changes

Then inspect:

```bash
podman compose logs -f pylon_main
```

and:

```bash
podman compose logs -f minio
```

---

### Need a clean rebuild
Run:

```bash
podman compose down -v
podman compose up -d
```

---

## 30. Daily workflow

### Start your day

```bash
podman machine start
cd ~/elitea/centry
podman compose up -d
open http://localhost
```

If MinIO is included and you want to verify it too:

```bash
open http://localhost:9001
```

---

### Check logs when needed

```bash
podman compose logs -f
```

---

### Stop at the end of the day

```bash
cd ~/elitea/centry
podman compose down
```

---

## 31. Command cheat sheet

### Podman
```bash
podman --version
podman machine list
podman machine start
podman run --rm hello-world
podman compose version
```

### Clone and open
```bash
mkdir -p ~/elitea
cd ~/elitea
git clone https://github.com/EliteaAI/centry.git
cd centry
code .
```

### Environment
```bash
cp envs/default.env envs/override.env
```

### Generate `SECRETS_MASTER_KEY`
```bash
podman run --rm --entrypoint= ghcr.io/eliteaai/pylon:1.2.25 \
  python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
```

### Start stack
```bash
podman compose pull
podman compose up -d
podman compose logs -f
```

### Check status
```bash
podman compose ps
podman ps
```

### Open local app
```bash
open http://localhost
```

### Open MinIO console
```bash
open http://localhost:9001
```

### Stop stack
```bash
podman compose down
```

### Full reset
```bash
podman compose down -v
podman compose pull
podman compose up -d
```

---

## 32. Example final minimal local configuration

### `envs/override.env`
```dotenv
APP_HOST=localhost
APP_PROTO=http

SECRETS_MASTER_KEY=<PASTE_GENERATED_KEY_HERE>

MINIO_ACCESS_KEY=minioadmin
MINIO_SECRET_KEY=<CHANGE_ME_MINIO_SECRET>
MINIO_REGION=us-east-1
MINIO_URL=http://minio:9000
```

### `docker-compose.yml` additional MinIO part
```yaml
  minio:
    image: minio/minio
    restart: unless-stopped
    logging:
      driver: "json-file"
      options:
        max-file: "5"
        max-size: "10m"
    env_file:
      - ./envs/default.env
      - ./envs/override.env
    command: server /data --console-address ":9001"
    volumes:
      - minio-data:/data
    ports:
      - "9000:9000"
      - "9001:9001"
    networks:
      - centry
```

### `volumes:` block
```yaml
volumes:
  redis-data:
  postgres-data:
  minio-data:
```

---

## 33. Summary

To run Elitea locally on macOS Apple Silicon using Podman:

1. verify Git, VSCode, and Podman
2. clone `EliteaAI/centry`
3. copy `envs/default.env` to `envs/override.env`
4. set:
   - `APP_HOST=localhost`
   - `APP_PROTO=http`
   - a generated `SECRETS_MASTER_KEY`
5. add MinIO environment values
6. add the MinIO service to `docker-compose.yml`
7. start the stack:
   ```bash
   podman compose pull
   podman compose up -d
   ```
8. open:
   ```text
   http://localhost
   ```
9. verify MinIO:
   ```text
   http://localhost:9001
   ```

