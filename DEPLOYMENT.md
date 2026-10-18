# VisualFlow Studio: production deployment

[Русская версия](DEPLOYMENT.ru.md) · [Project overview](README.md)

This guide targets Ubuntu 24.04 LTS and the recommended `gflow` provider.

## Production boundary

```text
Internet → Nginx :443 → authentication → 127.0.0.1:4000 → Docker
                                                   ├── SQLite/media
                                                   └── private Chrome profile
```

Never expose port 4000 on a public interface. The dashboard controls paid
generation and must be protected with authentication.

## Host packages

```bash
apt update
apt install -y docker.io docker-compose-v2 nginx certbot \
  python3-certbot-nginx apache2-utils fail2ban
```

Recommended host resources: 4 CPU, 8 GB RAM, and enough disk for the configured
retention window.

## Directory layout

```text
/opt/visualflow/
├── application source
├── config/             # private
├── data/               # private SQLite and media
├── logs/               # private
├── browser_profiles/   # private legacy profiles
└── gflow_profiles/     # private authenticated Flow profiles
```

```bash
install -d -m 0750 \
  /opt/visualflow/config \
  /opt/visualflow/data \
  /opt/visualflow/logs \
  /opt/visualflow/browser_profiles \
  /opt/visualflow/gflow_profiles
cp config/setting.example.toml config/setting.toml
```

Configure `provider_mode = "gflow"`, the profile name, and an existing Flow
project UUID. Do not commit the runtime configuration.

## Flow profile

Create the profile in an interactive desktop session:

```bash
gflow auth login --profile client --browser chrome
gflow auth status --profile client
```

Complete the Flow application sign-in, not only the Google account login. Stop
Chrome cleanly before copying or mounting the profile. A Chrome profile must be
used by only one running browser process.

The profile contains reusable session credentials. Store it with restrictive
permissions, exclude it from Git and Docker build contexts, and encrypt backups.

## Build and run

```bash
docker compose -f docker-compose.prod.yml config
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml ps
curl --fail http://127.0.0.1:4000/health
```

The Dockerfile applies source-guarded patches to the pinned gflow-cli release.
A patch mismatch must fail the build; do not bypass that failure without
reviewing the upstream change.

## Nginx and HTTPS

Create a bcrypt password file:

```bash
htpasswd -cB /etc/nginx/.htpasswd visualflow
chown root:www-data /etc/nginx/.htpasswd
chmod 0640 /etc/nginx/.htpasswd
```

Use `deploy/nginx/myflow.conf.example` as a starting point. Keep these
properties:

- upstream `http://127.0.0.1:4000`;
- Basic Auth or stronger identity-aware authentication;
- `client_max_body_size 100m`;
- request/response timeouts long enough for generation;
- no trust in client-supplied forwarding headers.

Validate before reload:

```bash
nginx -t
systemctl reload nginx
certbot --nginx -d visualflow.example.com
```

## Firewall and Fail2ban

```bash
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw default deny incoming
ufw default allow outgoing
ufw --force enable
```

Port 4000 must not appear in public allow rules. Ready-to-adapt Fail2ban
examples are stored in `deploy/fail2ban/`.

## Backups

Do not copy a live SQLite database directly while WAL mode is active. Use the
included systemd timer and `scripts/backup-myflow.sh`, or SQLite's online
backup API. A complete backup includes:

- an online SQLite snapshot;
- generated media still inside retention;
- runtime configuration;
- the encrypted browser profile, stored separately with limited access.

Test restoration regularly. Never publish a backup archive.

## Safe upgrades

1. Back up the database and runtime configuration.
2. Fetch the exact desired commit.
3. Export it to a fresh staging directory with `git archive`.
4. Build a separately tagged image.
5. Run tests and verify the expected compatibility patches.
6. Tag the current production image for rollback.
7. Synchronize source while excluding all runtime directories.
8. Rebuild, recreate the service, and wait for `/health`.
9. Verify the dashboard and one low-cost real generation.
10. Remove staging files and the rollback image only after success.

Do not deploy `.git`, profiles, credentials, databases, or generated media
from a developer checkout.

## Troubleshooting

| Symptom | Action |
|---|---|
| Profile locked | Stop all Chrome processes using that profile; remove only verified stale singleton markers |
| Authentication expired | Repeat interactive `gflow auth login` and replace the mounted profile safely |
| Prompt/reference guard refused submit | Keep the task failed; inspect technical details before retrying |
| Generated media has no local file | Use gflow catalog recovery or the browser-download fallback |
| Flow UI changed | Compare the pinned gflow release, update selectors/patches, rebuild, and run a real smoke test |
| No credits/quota | Wait for the account reset or use an independently authorized account; do not retry aggressively |

## Publication checklist

- full Git history scanned for secrets;
- any historically exposed credential revoked;
- only example configuration tracked;
- no client media, database, logs, HAR, incident bundle, or browser profile;
- MIT notice present and third-party notices reviewed;
- reverse proxy authentication enabled before sharing the dashboard.
