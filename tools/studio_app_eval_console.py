"""Console steps a person does for the Studio app eval: seed services and approve proposals.

The harness signs in with the install's admin password in a real browser, the way the
quickstart README tells a person to review and approve. None of this is visible to the agent.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

IMPORT_WAIT_MS = 180_000
PUBLISH_WAIT_MS = 120_000
SETTLE_MS = 2_500
TABLE_POLLS = 6


@dataclass(frozen=True)
class ServiceFixture:
    file: Path
    table: str
    service: str
    title: str


def sign_in(page, password: str) -> None:
    page.goto('/console/auth/signin')
    page.locator('input[type="password"]').fill(password)
    with page.expect_response(lambda r: r.url.endswith('/api/v1/admin/auth/session')
                              and r.request.method == 'POST') as response:
        page.get_by_role('button', name='Continue', exact=True).click()
    if response.value.status != 204:
        raise RuntimeError(f'Console sign-in returned {response.value.status}')
    page.wait_for_url(lambda url: '/auth/signin' not in url)


def import_file(page, fixture: ServiceFixture) -> str:
    page.goto('/console/operate/resources/import', wait_until='networkidle')
    page.locator('input[placeholder="parcels"]').fill(fixture.table)
    page.locator('input[type=file]').first.set_input_files(str(fixture.file))
    page.locator('[data-import-coordinate-system], [role=alert]').first.wait_for(timeout=60_000)
    page.get_by_role('button', name='Import file').click()
    page.locator('[data-imported-table], .console-state-danger').first.wait_for(timeout=IMPORT_WAIT_MS)
    imported = page.locator('[data-imported-table]').inner_text().strip()
    if not imported:
        raise RuntimeError(f'import of {fixture.file.name} did not report a table')
    return imported


def _table_option(page, table: str, polls: int) -> str | None:
    tables = page.locator('select[aria-label=Table] option')
    for _ in range(polls):
        page.wait_for_timeout(SETTLE_MS)
        match = next((v for v in tables.evaluate_all('os => os.map(o => o.value)') if table in v), None)
        if match:
            return match
    return None


def _select_table(page, table: str) -> None:
    connection = page.locator('select[aria-label=Connection]')
    connection.locator('option[value]:not([value=""])').first.wait_for(state='attached')
    for value in connection.locator('option').evaluate_all('os => os.map(o => o.value).filter(Boolean)'):
        connection.select_option(value)
        match = _table_option(page, table, TABLE_POLLS)
        if match:
            page.locator('select[aria-label=Table]').select_option(match)
            return
    raise RuntimeError(f'imported table {table} is not offered for publishing')


def publish(page, fixture: ServiceFixture, table: str) -> None:
    page.goto('/console/operate/publishing/quick', wait_until='networkidle')
    _select_table(page, table)
    page.locator('[data-workspace-field=serviceName]').fill(fixture.service)
    page.locator('[data-workspace-field=layerName]').fill(fixture.title)
    page.locator('[data-workspace-publish]').click()
    page.locator('[data-published-service], .console-state-danger').first.wait_for(timeout=PUBLISH_WAIT_MS)
    if not page.locator('[data-published-service]').count():
        raise RuntimeError(f'publishing {fixture.service} failed')


def allow_anonymous_read(page, service: str) -> None:
    page.goto(f'/console/operate/services/{service}/settings', wait_until='networkidle')
    page.get_by_role('tab', name='Access', exact=True).click()
    page.get_by_label('Allow anonymous read').check()
    page.get_by_role('button', name='Save').first.click()
    page.wait_for_timeout(SETTLE_MS)
    page.reload(wait_until='networkidle')
    page.get_by_role('tab', name='Access', exact=True).click()
    if not page.get_by_label('Allow anonymous read').is_checked():
        raise RuntimeError(f'anonymous read did not persist for {service}')


def seed_services(page, fixtures: list[ServiceFixture]) -> list[dict]:
    seeded = []
    for fixture in fixtures:
        table = import_file(page, fixture)
        publish(page, fixture, table)
        allow_anonymous_read(page, fixture.service)
        seeded.append({'service': fixture.service, 'title': fixture.title, 'table': table})
    return seeded


def approve_in_inbox(page, proposal_id: str) -> int:
    page.goto(f'/console/inbox?proposalId={proposal_id}', wait_until='networkidle')
    with page.expect_response(lambda r: r.url.endswith('/approve') and r.request.method == 'POST') as response:
        page.get_by_role('button', name='Approve').first.click()
        confirm = page.locator('dialog[open], [role=dialog]').get_by_role('button', name='Approve')
        if confirm.count():
            confirm.first.click()
    return response.value.status
