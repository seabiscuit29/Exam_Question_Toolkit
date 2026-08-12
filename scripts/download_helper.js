// Safe DOM helper template for an already approved question-bank page.
// Run individual functions only after read-only inspection and user confirmation.

function normalizeLabel(value) {
  return String(value || '').replace(/\s+/g, '').trim();
}

function isAllowedPath(pathname, configuredPrefix) {
  const prefix = String(configuredPrefix || '');
  if (!prefix.startsWith('/')) return false;
  const exact = prefix.length > 1 && prefix.endsWith('/') ? prefix.slice(0, -1) : prefix;
  const descendants = prefix.endsWith('/') ? prefix : `${prefix}/`;
  return pathname === exact || pathname.startsWith(descendants);
}

function assertApprovedLocation(config) {
  if (location.protocol !== 'https:') throw new Error('Only HTTPS pages are allowed');
  if (location.origin !== config.approvedOrigin) throw new Error('Origin changed');
  if (!isAllowedPath(location.pathname, config.allowedPathPrefix)) throw new Error('Path left approved scope');
}

function requireSingleVisible(selector, label) {
  const nodes = Array.from(document.querySelectorAll(selector)).filter((node) => {
    const style = getComputedStyle(node);
    return style.display !== 'none' && style.visibility !== 'hidden';
  });
  if (nodes.length !== 1) throw new Error(`${label} must match exactly one visible element`);
  return nodes[0];
}

function createCollectionBudget(limits) {
  const required = ['maxPages', 'maxDurationMs', 'maxClicks', 'maxBytes', 'maxFailures'];
  required.forEach((name) => {
    if (!Number.isSafeInteger(limits[name]) || limits[name] <= 0) {
      throw new Error(`Invalid collection limit: ${name}`);
    }
  });
  return {
    limits: {...limits},
    startedAt: Date.now(),
    pages: 0,
    clicks: 0,
    bytes: 0,
    failures: 0,
    lastPage: null,
    states: new Set()
  };
}

function assertTimeBudget(budget) {
  if (Date.now() - budget.startedAt > budget.limits.maxDurationMs) {
    throw new Error('Duration budget exceeded');
  }
}

function recordPage(budget, state) {
  assertTimeBudget(budget);
  if (!Number.isSafeInteger(state.page) || state.page <= 0) throw new Error('Invalid page number');
  if (!Number.isSafeInteger(state.bytes) || state.bytes < 0) throw new Error('Invalid page byte count');
  if (!/^[a-f0-9]{64}$/i.test(state.sha256)) throw new Error('Invalid SHA-256 state');
  if (budget.lastPage !== null && state.page !== budget.lastPage + 1) {
    throw new Error('Page sequence is not strictly monotonic');
  }
  const key = `${new URL(state.url).href}|${state.page}|${state.sha256.toLowerCase()}`;
  if (budget.states.has(key)) throw new Error('Repeated collection state');
  if (budget.pages + 1 > budget.limits.maxPages) throw new Error('Page budget exceeded');
  if (budget.bytes + state.bytes > budget.limits.maxBytes) throw new Error('Byte budget exceeded');
  budget.states.add(key);
  budget.pages += 1;
  budget.bytes += state.bytes;
  budget.lastPage = state.page;
  budget.failures = 0;
}

function recordClicks(budget, count) {
  assertTimeBudget(budget);
  if (!Number.isSafeInteger(count) || count < 0) throw new Error('Invalid click count');
  if (budget.clicks + count > budget.limits.maxClicks) throw new Error('Click budget exceeded');
  budget.clicks += count;
}

function recordFailure(budget) {
  assertTimeBudget(budget);
  budget.failures += 1;
  if (budget.failures >= budget.limits.maxFailures) throw new Error('Failure budget exceeded');
}

async function measureContent(text) {
  const bytes = new TextEncoder().encode(String(text));
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  const sha256 = Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, '0')).join('');
  return {bytes: bytes.byteLength, sha256};
}

function inspectPage(config) {
  assertApprovedLocation(config);
  const root = requireSingleVisible(config.questionContainer, 'question container');
  const items = Array.from(root.querySelectorAll(config.questionItem));
  const pager = requireSingleVisible(config.paginationContainer, 'pagination container');
  const buttons = Array.from(root.querySelectorAll(config.submitButton));
  const next = pager.querySelector(config.nextLink);
  return {
    origin: location.origin,
    path: location.pathname,
    questionCount: items.length,
    submitButtons: buttons.map((b) => normalizeLabel(b.textContent)),
    nextUrl: next ? new URL(next.href, location.href).href : null
  };
}

function submitVerifiedAnswers(config, maxClicks) {
  assertApprovedLocation(config);
  const root = requireSingleVisible(config.questionContainer, 'question container');
  const allowed = new Set(config.submitLabels.map(normalizeLabel));
  const buttons = Array.from(root.querySelectorAll(config.submitButton));
  if (buttons.length > maxClicks) throw new Error('Click budget exceeded');
  buttons.forEach((button) => {
    const tag = button.tagName.toLowerCase();
    const type = normalizeLabel(button.getAttribute('type')).toLowerCase();
    const label = normalizeLabel(button.textContent);
    if (tag !== 'button') throw new Error('Unexpected submit element');
    if (type !== 'button') throw new Error('Form-submitting button rejected');
    if (!allowed.has(label)) throw new Error(`Unexpected button label: ${label}`);
    if (button.disabled) return;
    button.click();
  });
  return buttons.length;
}

function extractQuestions(config) {
  assertApprovedLocation(config);
  const root = requireSingleVisible(config.questionContainer, 'question container');
  return Array.from(root.querySelectorAll(config.questionItem)).map((item) => ({
    question: (item.querySelector(config.stem)?.innerText || '').trim(),
    options: Array.from(item.querySelectorAll(config.options)).map((node) => node.innerText.trim()),
    answer: (item.querySelector(config.answer)?.innerText || '').trim(),
    knowledge: (item.querySelector(config.knowledge)?.innerText || '').trim(),
    analysis: (item.querySelector(config.analysis)?.innerText || '').trim()
  }));
}

function getVerifiedNextUrl(config, currentPage) {
  assertApprovedLocation(config);
  const pager = requireSingleVisible(config.paginationContainer, 'pagination container');
  const link = pager.querySelector(config.nextLink);
  if (!link) return null;
  const url = new URL(link.href, location.href);
  if (url.origin !== config.approvedOrigin) throw new Error('Cross-origin next link rejected');
  if (!isAllowedPath(url.pathname, config.allowedPathPrefix)) throw new Error('Next path left approved scope');
  const nextPage = Number(url.searchParams.get(config.pageParam));
  if (!Number.isSafeInteger(nextPage) || nextPage !== currentPage + 1) {
    throw new Error('Next page is not strictly monotonic');
  }
  return url.href;
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    normalizeLabel,
    isAllowedPath,
    assertApprovedLocation,
    createCollectionBudget,
    recordPage,
    recordClicks,
    recordFailure,
    measureContent,
    inspectPage,
    submitVerifiedAnswers,
    extractQuestions,
    getVerifiedNextUrl
  };
}
