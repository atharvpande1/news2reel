# Feedcast

Turns a city's newspaper RSS feeds into Instagram carousels. Feedcast polls each
city's feeds, keeps the stories that are local news, ranks them by how well they
would post, and lets an editor pick up to eight, edit the slides on a canvas and
download the carousel as JPEGs. Every carousel stays in the Gallery. Nothing is
published automatically; a person still posts.

Design decisions and invariants live in [CLAUDE.md](CLAUDE.md).

## Local development

Requires Docker with Compose v2.

```bash
cp .env.local.example .env.local      # fill in FEEDCAST_LLM_API_KEY and FEEDCAST_JWT_SECRET
docker compose up --build             # Postgres, migrations, then uvicorn --reload on 127.0.0.1:8000
docker compose exec api pytest        # uses the throwaway feedcast_test database
```

Open <http://127.0.0.1:8000/ui/>. There is no signup: add a user by hand (below).

## Deploy (AlmaLinux + Docker CE + nginx)

One VPS: the two compose containers (`api`, `db`) on loopback, nginx on the
host for TLS. Run as root or with sudo; replace `feedcast.example.com`.

**1. Docker CE**

```bash
dnf -y install dnf-plugins-core
dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
dnf -y install docker-ce docker-ce-cli containerd.io docker-compose-plugin
systemctl enable --now docker
```

**2. The app**

```bash
git clone <repo> /opt/feedcast && cd /opt/feedcast
cp .env.prod.example .env.prod && chmod 600 .env.prod
python3 -c "import secrets;print(secrets.token_urlsafe(48))"   # → FEEDCAST_JWT_SECRET
python3 -c "import secrets;print(secrets.token_urlsafe(24))"   # → POSTGRES_PASSWORD, and inside FEEDCAST_DATABASE_URL
vi .env.prod                                                   # also FEEDCAST_LLM_API_KEY

# Pin the prod compose files. Without this, a bare `docker compose` also loads
# docker-compose.override.yml — the local bind mount, --reload and .env.local.
echo "COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml" > .env

docker compose up -d --build
curl -s 127.0.0.1:8000/health          # {"status":"ok"}
```

The API refuses to start without a JWT secret of 32+ characters. Migrations run
on every container start.

**3. TLS and nginx**

```bash
dnf -y install epel-release
dnf -y install nginx certbot
# Issue the certificate before nginx has a config that points at it. The hooks
# are saved with the certificate, so renewals stop and start nginx themselves.
certbot certonly --standalone -d feedcast.example.com \
  --pre-hook "systemctl stop nginx" --post-hook "systemctl start nginx"
systemctl enable --now certbot-renew.timer

cp deploy/nginx.conf /etc/nginx/conf.d/feedcast.conf
sed -i 's/feedcast.example.com/<your domain>/g' /etc/nginx/conf.d/feedcast.conf
# SELinux: without this Alma's nginx may not open the proxy connection, and
# every request is a 502.
setsebool -P httpd_can_network_connect 1
nginx -t && systemctl enable --now nginx

firewall-cmd --permanent --add-service=http --add-service=https
firewall-cmd --reload
```

Do not open 8000 or 5432 in the firewall. The containers publish on 127.0.0.1
only, and nothing else should reach them.

**4. The first user**

```bash
docker compose exec api python -c "from argon2 import PasswordHasher; print(PasswordHasher().hash(input('password: ')))"
docker compose exec db psql -U news2reel -c \
  "insert into users (email, password_hash) values (lower('you@example.com'), '<hash>')"
```

**5. Backups (manual for now)**

The `articles` table is the dedupe ledger. Feeds carry only a recent window, so
it cannot be rebuilt: back it up.

```bash
docker compose exec -T db pg_dump -U news2reel -Fc news2reel > feedcast-$(date +%F).dump
# restore into the running database (replaces what is there):
docker compose exec -T db pg_restore -U news2reel -d news2reel --clean --if-exists < feedcast-YYYY-MM-DD.dump
```

**6. Updating**

```bash
cd /opt/feedcast && git pull && docker compose up -d --build
```

Logs: `docker compose logs -f api`. Each container's log is rotated at
10MB × 5.
