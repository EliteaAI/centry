# Local deployment for EliteA


## Requirements
- Docker
- Docker compose (docker-compose-plugin preferred)


## Configuration
1. Copy envs/default.env to envs/override.env


2. Edit envs/override.env:
    - Set APP_HOST to IP/DNS of platform
    - Change passwords/keys (for production, use default ones for dev at your own risk)
    > Special command for SECRETS_MASTER_KEY:
    > ```sh
    > docker run --rm --entrypoint= ghcr.io/eliteaai/pylon:1.2.25 python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
    > ```


3. SSL (optional): copy your server.crt, server.key, ca.crt to ssl/, then:
    - edit pylon_*/pylon.yml: uncomment SSL-related settings
    - edit docker-compose.yml: uncommend SSL-related settings
    - edit envs/override.env: set APP_PROTO to https


4. OpenID Connect SSO (optional):
    - edit pylon_auth/configs/auth_core.yml: set auth_provider to oidc
    - edit pylon_auth/configs/auth_oidc.yml: insert config for your OIDC provider
    - edit pylon_auth/configs/auth_init.yml: specify ID from your provider (e.g. email) for default platform admin


## Start
```sh
docker compose pull
docker compose up -d
docker compose logs -f
```


## First-time platform configuration
By default platform will perform auto-setup, so only adding AI (LLM, Embedding) configurations is needed.
