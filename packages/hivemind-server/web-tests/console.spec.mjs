import { test, expect } from '@playwright/test';

test('console themes persist through login and reload and keep presence distinct', async ({ page }) => {
  await page.goto(process.env.HIVEMIND_TEST_UI_URL);
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'blue');
  await expect(page.locator('#login-theme')).toHaveValue('blue');
  const background = () => page.locator('body').evaluate(el => getComputedStyle(el).backgroundColor);
  expect(await background()).toBe('rgb(13, 21, 35)');
  await page.locator('#login-theme').selectOption('red');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'red');
  expect(await background()).toBe('rgb(16, 13, 16)');
  await expect(page.locator('body')).toHaveCSS('font-family', /Rajdhani/);
  await expect(page.locator('#login-screen h1')).toHaveCSS('font-family', /Oxanium/);
  await expect(page.locator('#login-screen .brand-mark')).toHaveCSS(
    'background-image', /red-emblem\.svg/);
  await expect(page.locator('.orbit-center')).toHaveCSS(
    'background-image', /red-emblem\.svg/);
  const loadedFonts = await page.evaluate(async () => (await Promise.all([
    document.fonts.load('700 32px Oxanium'), document.fonts.load('600 16px Rajdhani')
  ])).map(faces => faces.length));
  expect(loadedFonts).toEqual([1, 1]);
  await page.reload();
  await expect(page.locator('#login-theme')).toHaveValue('red');
  expect(await background()).toBe('rgb(16, 13, 16)');
  await page.locator('#login-token').fill(process.env.HIVEMIND_TEST_TOKEN);
  await page.locator('#login-form button').click();
  await expect(page.locator('#toolbar-theme')).toHaveValue('red');
  await expect(page.locator('.panel').first()).toHaveCSS('background-color', 'rgb(35, 25, 28)');
  const online = await page.evaluate(() => {
    const badge = document.createElement('span'); badge.className = 'chip online';
    document.body.append(badge);
    const color = getComputedStyle(badge).color;
    badge.remove();
    return color;
  });
  const [red, green] = online.match(/\d+/g).map(Number);
  expect(green).toBeGreaterThan(red);
  await page.setViewportSize({width: 390, height: 844});
  await expect(page.locator('#toolbar-theme')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.locator('#toolbar-theme').selectOption('blue');
  expect(await background()).toBe('rgb(13, 21, 35)');
  await expect(page.locator('#app-shell .brand-mark')).not.toHaveCSS(
    'background-image', /red-emblem\.svg/);
  await page.reload();
  await expect(page.locator('#toolbar-theme')).toHaveValue('blue');
  expect(await page.evaluate(() => [localStorage.length,
    localStorage.getItem('hivemind-console-theme')])).toEqual([1, 'blue']);
});

test('token login, teams, tasks and mobile controls', async ({ page }) => {
  const base = process.env.HIVEMIND_TEST_UI_URL;
  await page.goto(base);
  await expect(page.getByText('Your projects.')).toBeVisible();
  await page.locator('#login-token').fill(process.env.HIVEMIND_TEST_TOKEN);
  await page.locator('#login-form button').click();
  await expect(page).toHaveTitle('Hivemind · Orchestrator Console');
  await expect(page.locator('#page-title')).toHaveText('Overview');
  await expect(page.locator('#project-switcher')).toHaveValue('default');
  await expect(page.locator('#server-version')).toHaveText(/^v\d+\.\d+\.\d+/);
  await page.getByRole('button', {name: 'Rooms', exact: true}).click();
  await page.locator('#room-create-form [name=name]').fill('release-review');
  await page.locator('#room-create-form [name=description]').fill('Coordinating the 1.5.3 release');
  await page.locator('#room-create-form button').click();
  await expect(page.locator('#room-list')).toContainText('release-review');
  await page.locator('#member-form [name=address_user]').fill('nikt');
  await page.locator('#member-form [name=address_device]').fill('macbook');
  await page.locator('#member-form [name=address_client]').fill('codex');
  await page.locator('#member-form button').click();
  await page.locator('#manager-form [name=address_user]').fill('nikt');
  await page.locator('#manager-form [name=address_device]').fill('macbook');
  await page.locator('#manager-form [name=address_client]').fill('codex');
  await page.locator('#manager-form button').click();
  await expect(page.locator('#room-list')).toContainText('Manager: nikt · macbook · codex');
  await expect(page.locator('#capability-create-form')).toBeHidden();
  await page.getByRole('button', {name: 'Capabilities', exact: true}).click();
  for(const [tag, description] of [['review', 'Review code for correctness'],
                                    ['python', 'Implement Python changes']]) {
    await page.locator('#capability-create-form [name=name]').fill(tag);
    await page.locator('#capability-create-form [name=description]').fill(description);
    await page.locator('#capability-create-form button').click();
    await expect(page.locator('#capability-catalog')).toContainText(description);
  }
  await page.getByRole('button', {name: 'Agents', exact: true}).click();
  await page.locator('#capability-agent').selectOption('["nikt","macbook","codex"]');
  await page.locator('#capability-choices input[value=review]').check();
  await page.locator('#capability-choices input[value=python]').check();
  await page.locator('#capability-assign-form button').click();
  await page.locator('#agent-config-parallel').fill('3');
  await page.locator('#agent-config-form button').click();
  await expect(page.locator('#agent-config-parallel')).toHaveValue('3');
  await page.getByRole('button', {name: 'Tasks', exact: true}).click();
  await page.locator('#task-create-form [name=title]').fill('Check release notes');
  await page.locator('#task-create-form [name=summary]').fill('Review documentation');
  await page.locator('#task-create-form button').click();
  await expect(page.locator('#task-list')).toContainText('Check release notes');
  await expect(page.locator('#task-counts')).toContainText('1 Unclaimed');
  await expect(page.locator('#task-list details .body')).toBeHidden();
  await page.locator('#task-list summary').first().click();
  await expect(page.locator('#task-list details .body')).toBeVisible();
  await page.locator('#task-status-filter').selectOption('complete');
  await expect(page.locator('#task-list')).not.toContainText('Check release notes');
  await expect(page.locator('#task-counts')).toContainText('1 Unclaimed');
  await expect(page.locator('#task-select')).toContainText('Check release notes');
  await page.locator('#task-status-filter').selectOption('all');
  await page.locator('#task-create-form [name=title]').fill('Manager follow-up');
  await page.locator('#task-create-form [name=summary]').fill('Verify the release plan');
  await page.locator('#task-create-form [name=assign_to_manager]').check();
  await page.locator('#task-create-form button').click();
  await expect(page.locator('#task-counts')).toContainText('1 Assigned');
  await page.getByRole('button', {name: 'Overview', exact: true}).click();
  await expect(page.locator('#overview-task-counts')).toContainText('1 Unclaimed');
  await expect(page.locator('#overview-task-list')).toContainText('Check release notes');
  await page.getByRole('button', {name: 'Instructions', exact: true}).click();
  await page.locator('#instruction-form [name=to_manager]').check();
  await page.locator('#instruction-form [name=body]').fill('Queue up the release reviews');
  await page.locator('#instruction-form button').click();
  await expect(page.locator('#instruction-list')).toContainText('Queue up the release reviews');
  await page.getByRole('button', {name: 'Agents', exact: true}).click();
  await expect(page.getByText('DMs are visible')).toBeVisible();
  await expect(page.locator('#agent-list')).toContainText('python');
  const actionGap = await page.locator('#agent-list .agent-actions').first().evaluate(
    el => parseFloat(getComputedStyle(el).columnGap));
  expect(actionGap).toBeGreaterThanOrEqual(8);
  await page.locator('#agent-list button[aria-label="DM nikt · macbook · codex"]').click();
  await expect(page.locator('#dm-recipient')).toHaveValue('["nikt","macbook","codex"]');
  await page.locator('#dm-form [name=body]').fill('Ready for review');
  await page.locator('#dm-form button').click();
  await page.locator('#dm-form [name=body]').fill('Follow-up from human');
  await page.locator('#dm-form button').click();
  await page.locator('#dm-destination').selectOption('room');
  await page.locator('#dm-room').selectOption('release-review');
  await page.locator('#dm-form [name=body]').fill('Room update from human');
  await page.locator('#dm-form button').click();
  await page.getByRole('button', {name: 'Messages', exact: true}).click();
  await expect(page.locator('#message-list')).toContainText('Room update from human');
  await page.locator('#message-channel').selectOption('dm');
  await expect(page.locator('#message-list')).toContainText('Ready for review');
  await expect(page.locator('#message-list .item-card').first()).toContainText('Follow-up from human');
  await expect(page.locator('#message-list .item-card').first()).toContainText('nikt · human');
  await expect(page.locator('#message-list details .body').first()).toBeHidden();
  await page.locator('#message-list summary').first().click();
  await expect(page.locator('#message-list details .body').first()).toBeVisible();
  await page.getByRole('button', {name: 'Capabilities', exact: true}).click();
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', {name: 'Delete capability review'}).click();
  await expect(page.locator('#capability-catalog')).not.toContainText('review');
  const badgeColors = await page.evaluate(() => {
    const online = document.createElement('span'), offline = document.createElement('span');
    online.className = 'chip online'; offline.className = 'chip offline';
    document.body.append(online, offline);
    const colors = [getComputedStyle(online).color, getComputedStyle(offline).color];
    online.remove(); offline.remove();
    return colors;
  });
  assertNotEqualColor(badgeColors[0], badgeColors[1]);
  await page.setViewportSize({width: 390, height: 844});
  await expect(page.locator('#project-switcher')).toBeVisible();
  await expect(page.locator('#page-title')).toBeVisible();
  expect(await page.evaluate(() => localStorage.length)).toBe(0);
});

function assertNotEqualColor(green, red) {
  const [gr,gg] = green.match(/\d+/g).map(Number);
  const [rr,rg] = red.match(/\d+/g).map(Number);
  expect(gg).toBeGreaterThan(gr);
  expect(rr).toBeGreaterThan(rg);
}

test('logout and second login cannot retain another private project task', async ({ page }) => {
  await page.goto(process.env.HIVEMIND_TEST_UI_URL);
  await page.locator('#login-token').fill(process.env.HIVEMIND_TEST_TOKEN);
  await page.locator('#login-form button').click();
  await expect(page.locator('#project-switcher')).toContainText('nikt.private');
  await page.locator('#project-switcher').selectOption('nikt.private');
  await page.getByRole('button', { name: 'Rooms', exact: true }).click();
  await page.locator('#room-create-form [name=name]').fill('private-review');
  await page.locator('#room-create-form [name=description]').fill('Nikt-only work');
  await page.locator('#room-create-form button').click();
  await page.getByRole('button', { name: 'Tasks', exact: true }).click();
  await page.locator('#task-create-form [name=title]').fill('A_ONLY_SECRET_TASK');
  await page.locator('#task-create-form [name=summary]').fill('Private details');
  await page.locator('#task-create-form button').click();
  await expect(page.locator('#task-list')).toContainText('A_ONLY_SECRET_TASK');
  await page.locator('#task-create-form [name=summary]').fill('PRIVATE_UNSENT_DRAFT');
  await page.locator('#instruction-form [name=body]').fill('PRIVATE_INSTRUCTION_DRAFT');
  await page.locator('#dm-form [name=body]').fill('PRIVATE_DM_DRAFT');
  await page.locator('#logout').click();
  await expect(page.locator('#login-screen')).toBeVisible();
  await page.locator('#login-token').fill(process.env.HIVEMIND_TEST_SECOND_TOKEN);
  await page.locator('#login-form button').click();
  await expect(page.locator('#project-switcher')).toHaveValue('ana.private');
  await expect(page.locator('#project-switcher')).not.toContainText('nikt.private');
  await page.getByRole('button', { name: 'Tasks', exact: true }).click();
  await expect(page.locator('#task-list')).not.toContainText('A_ONLY_SECRET_TASK');
  await expect(page.locator('#task-select')).not.toContainText('A_ONLY_SECRET_TASK');
  await expect(page.locator('#task-create-form [name=summary]')).toHaveValue('');
  await expect(page.locator('#instruction-form [name=body]')).toHaveValue('');
  await expect(page.locator('#dm-form [name=body]')).toHaveValue('');
});
