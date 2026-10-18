"""Adapt the reviewed gflow-cli release for unprivileged Docker Chrome.

The service runs as a non-root user with Docker's ``no-new-privileges``
security option. Chrome cannot create its namespace sandbox in that
environment. Playwright normally adds ``--no-sandbox``, but gflow removes that
default argument and Chrome exits before Flow opens.

Keep this as a small, version- and source-guarded image-build patch. If gflow's
launch code changes, the build must fail instead of modifying unknown source.
All former download compatibility patches were removed when upgrading to
gflow-cli 0.84: that release has its own migrated-host media handling, while
VisualFlow Studio retains its browser-download fallback for stranded videos.
"""

from __future__ import annotations

from importlib import metadata, util
from pathlib import Path


EXPECTED_VERSION = "0.84.0"


def replace_once(source: str, old: str, new: str, label: str) -> str:
    """Replace exactly one reviewed source fragment or fail the image build."""
    if source.count(old) != 1:
        raise RuntimeError(
            f"gflow-cli source no longer matches the reviewed {EXPECTED_VERSION} "
            f"layout: {label}"
        )
    return source.replace(old, new)


def main() -> None:
    installed_version = metadata.version("gflow-cli")
    if installed_version != EXPECTED_VERSION:
        raise RuntimeError(
            f"Unsupported gflow-cli version {installed_version}; "
            f"expected {EXPECTED_VERSION}"
        )

    package = util.find_spec("gflow_cli")
    if package is None or not package.submodule_search_locations:
        raise RuntimeError("Installed gflow_cli package was not found")

    target = Path(next(iter(package.submodule_search_locations))) / "api" / "client.py"
    source = target.read_text(encoding="utf-8")

    old_ignore = '''            "ignore_default_args": [
                "--enable-automation",
                "--no-sandbox",
            ],'''
    new_ignore = '''            "ignore_default_args": [
                "--enable-automation",
            ],'''
    old_args = '''            "args": [
                "--password-store=basic",'''
    new_args = '''            "args": [
                "--no-sandbox",
                "--password-store=basic",'''

    patched = replace_once(source, old_ignore, new_ignore, "Chrome default args")
    patched = replace_once(patched, old_args, new_args, "Chrome launch args")
    target.write_text(patched, encoding="utf-8")
    print(f"Patched {target} for unprivileged Docker Chrome")

    # Flow occasionally returns a successful maseQ upload as a valid response
    # whose outer batchexecute frame no longer decodes through parse_frames.
    # gflow 0.84 then discards an upload that Flow accepted. Fall back only
    # when the raw response contains exactly one non-project UUID; ambiguity is
    # still refused, so a workflow or unrelated id can never be bound as the
    # user's reference image.
    composer = target.parent / "transports" / "migrated_composer.py"
    composer_source = composer.read_text(encoding="utf-8")
    old_uuid_parser = '''def _first_uuid(text: str) -> str | None:
    """The first UUID in a ``batchexecute`` reply — for ``maseQ`` that is the new
    media id (``[media_id, project_id, …]``, measured 2026-09-05)."""
    for rid, payload in parse_frames(text):
        if rid != UPLOAD_RPC:
            continue
        m = UUID_RE.search(str(payload))
        if m:
            return m.group(0)
    return None'''
    new_uuid_parser = '''def _first_uuid(text: str, *, project_id: str | None = None) -> str | None:
    """Return a safely identified media UUID from a ``maseQ`` upload reply.

    Prefer the decoded ``maseQ`` frame. If Flow changes only the outer frame,
    accept the raw response solely when it has one unique UUID other than the
    known project id. Multiple candidates remain a hard refusal.
    """
    for rid, payload in parse_frames(text):
        if rid != UPLOAD_RPC:
            continue
        m = UUID_RE.search(str(payload))
        if m:
            return m.group(0)
    project = (project_id or "").lower()
    candidates = list(
        dict.fromkeys(
            match.group(0)
            for match in UUID_RE.finditer(text)
            if match.group(0).lower() != project
        )
    )
    return candidates[0] if len(candidates) == 1 else None'''
    composer_source = replace_once(
        composer_source,
        old_uuid_parser,
        new_uuid_parser,
        "maseQ media-id parser",
    )
    composer_source = replace_once(
        composer_source,
        "            media_id = _first_uuid(text)",
        "            media_id = _first_uuid(text, project_id=project_id)",
        "maseQ project-aware parser call",
    )

    # Upstream verifies the requested model and reference ids before allowing
    # ogiZ0b through, but not the prompt. A stale contenteditable can therefore
    # submit the previous instruction while the dashboard reports the new one.
    # Compare the JSON-escaped prompt with the actual request body and abort the
    # route before Flow acts if it is absent.
    composer_source = replace_once(
        composer_source,
        '''def _image_body_problem(
    body: str,
    reference_ids: tuple[str, ...],
    model: ImageModel | None = None,
) -> str | None:''',
        '''def _image_body_problem(
    body: str,
    reference_ids: tuple[str, ...],
    model: ImageModel | None = None,
    prompt: str | None = None,
) -> str | None:''',
        "image submit prompt guard signature",
    )
    composer_source = replace_once(
        composer_source,
        '''    if model is not None and not any(
        token in body for token in IMAGE_MODEL_WIRE_TOKENS.get(model, (model.value,))
    ):''',
        '''    escaped_prompt = json.dumps(prompt, ensure_ascii=False)[1:-1] if prompt else ""
    if escaped_prompt and escaped_prompt not in body:
        return (
            "migrated host: the image submit body does not carry the requested prompt "
            "— refusing to run with stale editor text"
        )
    if model is not None and not any(
        token in body for token in IMAGE_MODEL_WIRE_TOKENS.get(model, (model.value,))
    ):''',
        "image submit prompt assertion",
    )
    composer_source = replace_once(
        composer_source,
        '''async def _guard_image_submit(
    route: Any,
    request: Any,
    reference_ids: tuple[str, ...],
    model: ImageModel | None,
) -> str | None:''',
        '''async def _guard_image_submit(
    route: Any,
    request: Any,
    reference_ids: tuple[str, ...],
    model: ImageModel | None,
    prompt: str | None,
) -> str | None:''',
        "image submit route guard signature",
    )
    composer_source = replace_once(
        composer_source,
        "    problem = _image_body_problem(_post_data(request), reference_ids, model)",
        "    problem = _image_body_problem(_post_data(request), reference_ids, model, prompt)",
        "image submit route prompt check",
    )
    composer_source = replace_once(
        composer_source,
        "problem = _image_body_problem(_post_data(raw_request), reference_ids, request.model)",
        "problem = _image_body_problem(\n                _post_data(raw_request), reference_ids, request.model, request.prompt\n            )",
        "image submit observer prompt check",
    )
    composer_source = replace_once(
        composer_source,
        "problem = await _guard_image_submit(route, raw_request, reference_ids, request.model)",
        "problem = await _guard_image_submit(\n                route, raw_request, reference_ids, request.model, request.prompt\n            )",
        "image submit preflight prompt check",
    )
    composer.write_text(composer_source, encoding="utf-8")
    print("Patched gflow maseQ parsing and image-submit prompt verification")


if __name__ == "__main__":
    main()
