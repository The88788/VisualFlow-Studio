# VisualFlow Studio roadmap

[Русская версия](ROADMAP.ru.md)

This roadmap describes direction, not a delivery promise. Priorities are
driven by reproducible issues, available test accounts, and contributor time.

## Near term

- CI for Python tests, documentation links, and Docker build validation;
- safer first-run setup and profile health diagnostics;
- provider fixtures that validate Flow request/response parsing without credits;
- clearer queue position, progress, cancellation, and retry UX;
- audit and isolation of the legacy provider and duplicate ImageAPI service;
- repository screenshots and a short demonstration made with non-client media.

## Next

- multiple independent gflow profiles with explicit isolation and scheduling;
- role-based dashboard authentication and per-user task ownership;
- configurable retention by media type;
- side-by-side revision comparison and lineage view;
- export/import of reusable prompt presets;
- notifications for completion, expired authorization, and exhausted quota.

## Long term

- provider adapter interface with conformance tests;
- external job queue and worker separation;
- PostgreSQL/object-storage deployment profile;
- reproducible releases, migrations, and signed container images;
- optional localization beyond English and Russian.

Features intended to evade platform restrictions, conceal abuse, share account
sessions, or bypass credits are out of scope.
