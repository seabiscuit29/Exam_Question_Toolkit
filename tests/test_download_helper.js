'use strict';

const assert = require('assert');
const helper = require('../scripts/download_helper.js');

const config = {
  approvedOrigin: 'https://good.example',
  allowedPathPrefix: '/bank/',
  paginationContainer: '.pager',
  nextLink: 'a.next',
  pageParam: 'page',
  questionContainer: '.questions',
  submitButton: 'button.submit',
  submitLabels: ['提交']
};

global.location = {
  protocol: 'https:',
  origin: 'https://good.example',
  pathname: '/bank/list',
  href: 'https://good.example/bank/list?page=1'
};
global.getComputedStyle = () => ({display: 'block', visibility: 'visible'});

let nextHref = 'https://good.example/bank/list?page=2';
const pager = {querySelector: () => ({href: nextHref})};
const root = {querySelectorAll: () => []};
global.document = {
  querySelectorAll: (selector) => selector === '.pager' ? [pager] : [root]
};

assert.strictEqual(
  helper.getVerifiedNextUrl(config, 1),
  'https://good.example/bank/list?page=2'
);

nextHref = 'https://evil.example/bank/list?page=2';
assert.throws(() => helper.getVerifiedNextUrl(config, 1), /Cross-origin/);

nextHref = 'https://good.example/bank/list?page=9';
assert.throws(() => helper.getVerifiedNextUrl(config, 1), /strictly monotonic/);

global.location.pathname = '/bank-evil/list';
assert.throws(() => helper.assertApprovedLocation(config), /Path left/);
global.location.pathname = '/bank/list';

global.location.origin = 'https://evil.example';
assert.throws(() => helper.assertApprovedLocation(config), /Origin changed/);

const budget = helper.createCollectionBudget({
  maxPages: 2,
  maxDurationMs: 60_000,
  maxClicks: 5,
  maxBytes: 100,
  maxFailures: 3
});
helper.recordPage(budget, {
  url: 'https://good.example/bank/list?page=1',
  page: 1,
  sha256: 'a'.repeat(64),
  bytes: 40
});
helper.recordClicks(budget, 3);
assert.throws(() => helper.recordClicks(budget, 3), /Click budget/);
assert.throws(() => helper.recordPage(budget, {
  url: 'https://good.example/bank/list?page=3',
  page: 3,
  sha256: 'b'.repeat(64),
  bytes: 10
}), /strictly monotonic/);
helper.recordPage(budget, {
  url: 'https://good.example/bank/list?page=2',
  page: 2,
  sha256: 'b'.repeat(64),
  bytes: 40
});
assert.throws(() => helper.recordPage(budget, {
  url: 'https://good.example/bank/list?page=3',
  page: 3,
  sha256: 'c'.repeat(64),
  bytes: 10
}), /Page budget/);

console.log('download_helper security checks passed');
