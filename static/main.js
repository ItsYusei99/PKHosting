try { applyTheme(); cmdTplInit(); navA11y(); } catch (e) {}
(function initTab() {
  try {
    let t = tabFromHash();
    if (!t) {
      try {
        const s = localStorage.getItem('pkTab');
        if (s && KNOWN_TABS.includes(s)) {
          t = s;
          if (s === 'files') {
            const fp = localStorage.getItem('pkFs') || '';
            if (fp) fsPath = fp;
          }
        }
      } catch (e) {}
    }
    if (t) switchTab(t, true);
  } catch (e) {}
})();
window.addEventListener('load', () => {
  try {
    const t = tabFromHash();
    const cur = document.querySelector('.tab-content.active');
    if (t && (!cur || cur.id !== 'tab-' + t)) switchTab(t, true);
  } catch (e) {}
});
