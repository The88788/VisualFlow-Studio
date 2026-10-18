# Security policy

[Русская версия](SECURITY.ru.md)

## Supported version

Security fixes currently target the latest commit on the `dev` branch. This
project is pre-1.0 in operational maturity despite its product version.

## Reporting a vulnerability

Use a private GitHub Security Advisory for:

- authentication or authorization bypass;
- remote code execution, command injection, or unsafe file handling;
- SSRF or download-host allow-list bypass;
- exposure of Google sessions, cookies, account credentials, or signed URLs;
- cross-user access to prompts, references, results, or task history;
- a deployment default that exposes the dashboard publicly.

Do not include working credentials, an entire browser profile, a real Cookies
database, or client media. Provide the smallest redacted reproduction.

## Operational security

- Treat `gflow_profiles/` and `browser_profiles/` as credentials.
- Keep the service on loopback behind authenticated HTTPS.
- Use one browser process per profile and a non-root container user.
- Encrypt backups and test restoration.
- Rotate any secret that ever entered Git history.
- Pass optional legacy OAuth client credentials only through the process
  environment; never bake them into source code or container images.
- Review logs and incident bundles before sharing them.
- Keep generation concurrency conservative; retries can spend credits.
- Do not automate acceptance of rights declarations for media you do not own.

VisualFlow Studio uses unofficial browser automation. A successful health check
does not prove that Google Flow generation is currently compatible.
