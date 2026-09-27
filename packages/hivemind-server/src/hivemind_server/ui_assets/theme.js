// Run before the stylesheet is loaded so returning visitors never see the wrong theme flash.
const THEME_KEY = 'hivemind-console-theme';
function applyTheme(value, save = false) {
  const theme = value === 'red' || value === 'red-alert' ? 'red' : 'blue';
  document.documentElement.dataset.theme = theme;
  for (const picker of document.querySelectorAll('[data-theme-picker]')) picker.value = theme;
  if (save) {
    try { window.localStorage.setItem(THEME_KEY, theme); } catch (_) { /* Storage is optional. */ }
  }
}
let savedTheme;
try { savedTheme = window.localStorage.getItem(THEME_KEY); } catch (_) { /* Storage is optional. */ }
applyTheme(savedTheme, savedTheme === 'red-alert' || savedTheme === 'current');
document.addEventListener('DOMContentLoaded', () => {
  for (const picker of document.querySelectorAll('[data-theme-picker]'))
    picker.addEventListener('change', event => applyTheme(event.currentTarget.value, true));
  applyTheme(document.documentElement.dataset.theme);
});
