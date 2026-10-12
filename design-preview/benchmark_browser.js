// Paste into the preview's DevTools console, or evaluate in its browser tab.
// Existing HTML preview only; synthetic stress catalog is flat, NOT grouped/searchable.
// No API requests. Catalog and selection are restored even if a check fails.
(async () => {
  const originalCatalog = { ...previewModels };
  const originalModel = selectedModel;
  const originalThinking = selectedThinking;
  const originalView = current;
  const originalDraft = document.querySelector('textarea')?.value;
  const results = {
    scope: 'Existing HTML mockup; flat synthetic catalog. Not native Qt or full plugin.',
    timing: 'Synchronous DOM update plus forced layout; frame samples are next requestAnimationFrame, not compositor presentation.',
    environment: { userAgent: navigator.userAgent, viewport: [innerWidth, innerHeight], devicePixelRatio, visibility: document.visibilityState },
    cases: [],
    screens: []
  };
  const stats = values => {
    const sorted = [...values].sort((a, b) => a - b);
    return { samples: values.length, median_ms: sorted[Math.floor(sorted.length / 2)], p95_ms: sorted[Math.ceil(sorted.length * 0.95) - 1], max_ms: sorted.at(-1), raw_ms: values };
  };
  const layout = () => document.querySelector('#stage').getBoundingClientRect().height;
  const measure = (action, count = 100) => {
    const samples = [];
    for (let i = 0; i < count; i++) {
      const start = performance.now();
      action(i);
      layout();
      samples.push(performance.now() - start);
    }
    return stats(samples);
  };
  const nextFrame = () => new Promise(resolve => requestAnimationFrame(resolve));
  try {
    show('workspace');
    await nextFrame();
    const coldStart = performance.now();
    document.querySelector('.model-picker').open = true;
    layout();
    results.first_popup_open_ms = performance.now() - coldStart;
    for (const count of [3, 60, 1000, 5000]) {
      for (const id of Object.keys(previewModels)) delete previewModels[id];
      Object.assign(previewModels, originalCatalog);
      for (let i = 3; i < count; i++) {
        previewModels[`connection-${i % 20} / model-${String(i).padStart(5, '0')}`] = { levels: ['low', 'medium', 'high'], context: 200000 };
      }
      selectedModel = originalModel;
      selectedThinking = originalThinking;
      const buildStart = performance.now();
      refreshModelControls();
      layout();
      const catalogPopulateMs = performance.now() - buildStart;
      const picker = document.querySelector('.model-picker');
      const open = () => { picker.open = false; layout(); picker.open = true; };
      for (let i = 0; i < 10; i++) { open(); layout(); }
      const warmOpen = measure(open);
      const ids = Object.keys(previewModels);
      const changeModel = i => {
        const select = document.querySelector('#quick-model');
        select.value = ids[i % ids.length];
        select.dispatchEvent(new Event('change', { bubbles: true }));
      };
      const textarea = document.querySelector('textarea');
      textarea.value = 'benchmark draft must survive';
      const change = measure(changeModel);
      if (textarea !== document.querySelector('textarea') || textarea.value !== 'benchmark draft must survive') throw new Error('Model change rebuilt or lost the composer');
      selectedModel = 'o3-mini';
      selectedThinking = 'high';
      refreshModelControls();
      const thinking = measure(i => {
        const select = document.querySelector('#quick-thinking');
        select.value = ['low', 'medium', 'high'][i % 3];
        select.dispatchEvent(new Event('change', { bubbles: true }));
      });
      const frames = [];
      for (let i = 0; i < 30; i++) {
        await nextFrame();
        const start = performance.now();
        open();
        await nextFrame();
        frames.push(performance.now() - start);
      }
      results.cases.push({ models: count, catalog_populate_ms: catalogPopulateMs, warm_open: warmOpen, change_model: change, change_thinking: thinking, open_next_frame: stats(frames), draft_retained: true, dom_elements: document.getElementsByTagName('*').length });
    }
    for (const id of Object.keys(previewModels)) delete previewModels[id];
    Object.assign(previewModels, originalCatalog);
    selectedModel = originalModel;
    selectedThinking = originalThinking;
    for (const [id, label] of screens) {
      show(id);
      layout();
      const timing = measure(() => show(id), 30);
      const stage = document.querySelector('#stage');
      results.screens.push({ id, label, render: timing, horizontal_overflow: stage.scrollWidth > stage.clientWidth + 1 });
    }
    return results;
  } finally {
    for (const id of Object.keys(previewModels)) delete previewModels[id];
    Object.assign(previewModels, originalCatalog);
    selectedModel = originalModel;
    selectedThinking = originalThinking;
    show(originalView);
    if (originalDraft !== undefined && document.querySelector('textarea')) document.querySelector('textarea').value = originalDraft;
  }
})()
