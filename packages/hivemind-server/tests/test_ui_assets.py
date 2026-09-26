"""Console assets ship with accessible shell and strict browser defaults."""

import httpx
import pytest

from hivemind_server.ui_app import build_ui_app


@pytest.mark.anyio
async def test_ui_serves_login_and_accessible_project_shell(env):
    app, _, _ = env
    ui = build_ui_app(app.state.cfg, app.state.registry, app.state.identities)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ui),
                                 base_url="http://localhost") as client:
        shell = await client.get("/")
        css = await client.get("/assets/styles.css")
        js = await client.get("/assets/app.js")
    assert shell.status_code == css.status_code == js.status_code == 200
    assert "Content-Security-Policy" in shell.headers
    assert 'id="project-switcher"' in shell.text
    assert "Instructions" in shell.text and "DMs are visible" in shell.text
    assert "textContent" in js.text and "innerHTML" not in js.text
    assert 'id="assignee-select"' in shell.text
    assert 'id="assign-eligibility"' in shell.text
    assert 'function renderAssignmentCandidates()' in js.text
    assert 'id="agent-older"' in shell.text
    assert 'id="room-older"' in shell.text
    assert 'id="candidate-older"' in shell.text
    assert "S.rooms=r.rooms;S.roomOlder=r.older_cursor" in js.text
    assert "S.candidates=result.candidates" in js.text
    assert "preserveUnknown&&old&&!values.includes(old)" in js.text
    assert "S.candidates.push({...pinned,pinned:true})" in js.text
    assert "if(pinnedTask&&!S.tasks.some" in js.text
    assert "!selected||selected.disabled" in js.text
    assert "Selected member now lacks required tags" in js.text
    assert 'document.querySelectorAll("#main form")' in js.text
    assert "if(epoch!==S.epoch)return;" in js.text
    assert "const epoch=S.epoch,target=address(f)" in js.text
