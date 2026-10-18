# Contributing to VisualFlow Studio

[Русская версия](CONTRIBUTING.ru.md)

Thank you for helping improve VisualFlow Studio. The project automates a
changing third-party web application, so small, reviewable, evidence-backed
changes are more valuable than broad speculative rewrites.

## Before opening an issue

- Search existing issues and include the VisualFlow Studio and gflow-cli
  versions.
- Remove emails, cookies, profile paths, signed media URLs, prompts containing
  private data, and client media.
- For provider failures, include the public error code and a redacted technical
  message. Never upload a complete Chrome profile, Cookies database, HAR file,
  or incident bundle without reviewing every field.
- State whether the account uses `flow.google.com`, the requested model, and
  whether references were attached.

Use GitHub Security Advisories instead of public issues for vulnerabilities or
credential exposure. See [SECURITY.md](SECURITY.md).

## Development setup

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
npm install
```

Windows activation:

```powershell
.\.venv\Scripts\Activate.ps1
```

Run the focused unit tests:

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

Validate Python sources:

```bash
python -m compileall src scripts
```

For provider changes, also build the Docker image. The build-time gflow patcher
must succeed against the pinned version.

## Pull requests

1. Create a focused branch.
2. Keep secrets and generated artifacts out of commits.
3. Add or update tests for behaviour changes.
4. Update both English and Russian documentation.
5. Explain whether the change can spend Flow credits.
6. Describe real verification separately from mocked/unit tests.
7. Preserve fail-closed guards around model, prompt, reference, download-host,
   and file-integrity checks.

Do not silently loosen anti-corruption, authorization, host allow-list, file
size, or credit-spend protections.

## Commit style

Use a short imperative summary in English or Russian. Keep unrelated cleanup
in a separate commit. Examples:

```text
Исправлена проверка референсов перед генерацией
Add persistent image retry status
```

## Areas where help is welcome

- provider compatibility tests and fixtures with redacted data;
- dashboard accessibility and responsive layout;
- multi-profile isolation without sharing one Chrome profile;
- installation automation and reproducible deployment;
- observability, recovery, and cleanup correctness;
- translations and documentation;
- removal or isolation of obsolete legacy code.

By contributing, you agree that your contribution is provided under the
project's MIT License and that you have the right to submit it.
