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

    # The migrated reference picker now renders clickable options reliably,
    # while pressing Enter can dismiss the picker and clear every previously
    # attached chip on some cohorts. Choose the uniquely named uploaded asset
    # by click. A UUID is part of each display name, so this cannot silently
    # select another user asset with a similar human-readable name.
    composer_source = replace_once(
        composer_source,
        '''            offered = [t.strip() for t in await page.locator(PICKER_OPTION).all_text_contents()]
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(2500)''',
        '''            offered = [t.strip() for t in await page.locator(PICKER_OPTION).all_text_contents()]
            matching = page.locator(PICKER_OPTION).filter(has_text=name)
            if await matching.count():
                await matching.first.click(timeout=4000)
            else:
                await page.keyboard.press("Escape")
            await page.wait_for_timeout(2500)''',
        "reference picker click commit",
    )
    composer_source = replace_once(
        composer_source,
        '''            for _ in range(len(name) + 1):  # the '@' and the query it opened
                await page.keyboard.press("Backspace")''',
        '''            # Never backspace blindly: after a failed picker commit the
            # query may already be gone, in which case Backspace removes chips.
            # Only erase the exact query when it is still visibly at the tail.
            await page.keyboard.press("Escape")
            composer_text = await page.locator(COMPOSER).first.inner_text()
            query = f"@{name}"
            if composer_text.rstrip().endswith(query):
                for _ in range(len(query)):
                    await page.keyboard.press("Backspace")''',
        "reference query safe cleanup",
    )

    # Verify the prompt in the editor before submit. The ogiZ0b body does not
    # preserve image-edit prompts as one contiguous JSON string when mention
    # chips are present, so checking the raw request body rejects valid i2i
    # submissions. DOM verification still catches a stale editor before Flow
    # can spend quota, while upstream continues to verify model + reference ids
    # in the actual request body.
    composer_source = replace_once(
        composer_source,
        '''        await page.keyboard.insert_text(prompt)
        log.info("migrated.prompt_typed", chars=len(prompt))''',
        '''        await page.keyboard.insert_text(prompt)
        editor_text = await composer.inner_text()
        normalized_prompt = " ".join(prompt.split())
        normalized_editor = " ".join(editor_text.split())
        if normalized_prompt and normalized_prompt not in normalized_editor:
            raise UiSelectorDriftError(
                detail=(
                    "migrated host: the Flow editor does not contain the requested prompt "
                    "after typing — refusing to submit stale editor text"
                ),
            )
        log.info("migrated.prompt_typed", chars=len(prompt), verified=True)''',
        "image editor prompt verification",
    )
    composer.write_text(composer_source, encoding="utf-8")
    print("Patched gflow uploads, reference picker, and image-submit verification")


if __name__ == "__main__":
    main()
