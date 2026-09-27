"""Console assets ship with accessible shell and strict browser defaults."""

import xml.etree.ElementTree as ET

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
        theme_js = await client.get("/assets/theme.js")
        emblem = await client.get("/assets/red-emblem.svg")
        fonts = [await client.get("/assets/" + name) for name in (
            "oxanium-latin.woff2", "rajdhani-regular-latin.woff2",
            "rajdhani-semibold-latin.woff2", "rajdhani-bold-latin.woff2")]
        claude_logo = await client.get("/assets/claude.svg")
        codex_logo = await client.get("/assets/codex.svg")
    assert shell.status_code == css.status_code == js.status_code == 200
    assert theme_js.status_code == 200
    assert 'text/javascript' in theme_js.headers['content-type']
    assert all(font.status_code == 200 and font.content[:4] == b'wOF2'
               and 'font/woff2' in font.headers['content-type'] for font in fonts)
    assert emblem.status_code == 200 and 'image/svg+xml' in emblem.headers['content-type']
    svg = ET.fromstring(emblem.content)
    assert svg.tag == '{http://www.w3.org/2000/svg}svg'
    assert svg.attrib['aria-label'] == 'Red star'
    assert len(svg) == 1 and svg[0].tag == '{http://www.w3.org/2000/svg}path'
    red, green, blue = (int(svg[0].attrib['fill'][i:i+2], 16) for i in (1, 3, 5))
    assert red > green and red > blue
    assert not svg.findall('.//{http://www.w3.org/2000/svg}script')
    assert claude_logo.status_code == codex_logo.status_code == 200
    assert 'image/svg+xml' in claude_logo.headers['content-type']
    assert '<svg' in codex_logo.text
    assert "Content-Security-Policy" in shell.headers
    assert "font-src 'self'" in shell.headers["Content-Security-Policy"]
    assert 'id="project-switcher"' in shell.text
    assert "Instructions" in shell.text and "DMs are visible" in shell.text
    assert "textContent" in js.text and "innerHTML" not in js.text
    assert 'id="assignee-select"' in shell.text
    assert 'id="assign-eligibility"' in shell.text
    assert 'function renderAssignmentCandidates()' in js.text
    assert 'id="agent-older"' in shell.text
    assert 'id="room-older"' in shell.text
    assert 'id="candidate-older"' in shell.text
    assert 'id="task-counts"' in shell.text and 'id="task-status-filter"' in shell.text
    assert 'name="assign_to_manager"' in shell.text
    assert 'assign_to_manager:d.get("assign_to_manager")==="on"' in js.text
    assert 'id="capability-create-form"' in shell.text
    assert 'id="capability-assign-form"' in shell.text
    assert 'id="capability-catalog"' in shell.text
    assert 'id="agent-config-form"' in shell.text
    assert 'id="dm-destination"' in shell.text and 'id="dm-recipient"' in shell.text
    assert 'id="dm-room"' in shell.text and 'id="instruction-recipient"' in shell.text
    assert "S.rooms=r.rooms;S.roomOlder=r.older_cursor" in js.text
    assert "S.candidates=result.candidates" in js.text
    assert "preserveUnknown&&old&&!values.includes(old)" in js.text
    assert "S.candidates.push({...pinned,pinned:true})" in js.text
    assert "!selected||selected.disabled" in js.text
    assert "Selected member now lacks required tags" in js.text
    assert 'document.querySelectorAll("#main form")' in js.text
    assert "if(epoch!==S.epoch)return;" in js.text
    assert "const epoch=S.epoch,target=address(f)" in js.text


def test_the_id_helper_is_never_handed_a_css_selector():
    """`$` is getElementById; `q` is querySelector. Three call sites passed `$` a selector, so it
    returned null and the first addEventListener on one threw out of init() — before any form,
    nav button, refresh timer or session resume was bound. The whole console was inert and login
    fell through to a native form submit. Nothing caught it: the assets tests grep for substrings
    and the Playwright spec needs npm plus a live server, so this guard is the cheap stand-in."""
    import pathlib
    import re
    from hivemind_server import ui_app
    source = (pathlib.Path(ui_app.__file__).parent / "ui_assets" / "app.js").read_text()
    offenders = re.findall(r"\$\(\s*['\"][#.\[][^'\"]*['\"]", source)
    assert offenders == [], f"$ takes an element id, not a selector — use q(): {offenders}"


@pytest.mark.anyio
async def test_the_theme_token_agrees_between_the_script_and_the_stylesheet(env):
    """theme.js writes a token into data-theme and styles.css is the only thing that reads it, so
    nothing but a browser notices when the two disagree. 89f6026 renamed red-alert -> red in both;
    had it missed the stylesheet, every pytest and node test would still have passed and Red would
    have shipped as a silent no-op identical to Blue. The Playwright spec that would catch it needs
    npm and a live server, which is exactly the gap this closes.
    """
    import re
    app, _, _ = env
    ui = build_ui_app(app.state.cfg, app.state.registry, app.state.identities)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ui),
                                 base_url="http://localhost") as client:
        css = await client.get("/assets/styles.css")
        script = await client.get("/assets/theme.js")
        shell = await client.get("/")
        licence = await client.get("/assets/FONTS-LICENSE")
    written = set(re.findall(r"""['"]([a-z-]+)['"]\s*:\s*['"]blue['"]""", script.text)) | \
        set(re.findall(r"value === '([a-z-]+)'", script.text))
    applied = set(re.findall(r'\[data-theme="([a-z-]+)"\]', css.text))
    assert "red" in applied, "the stylesheet defines no Red rules; the theme would be a no-op"
    for token in ("red",):
        assert token in written, f"theme.js no longer produces {token!r}"
    # every token the stylesheet styles must be one the script can actually emit
    assert applied <= {"red", "blue"}, applied
    # picker options must offer exactly the tokens the script accepts
    assert '<option value="red">' in shell.text and '<option value="blue">' in shell.text
    # the OFL notice is reachable beside the fonts it covers
    assert licence.status_code == 200 and "Open Font License" in licence.text
    assert "MODIFIED" in licence.text, "subset builds must not be described as unmodified"
