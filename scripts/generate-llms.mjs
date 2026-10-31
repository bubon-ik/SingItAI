// Dependency-free: website CI runs on a clean Node checkout without npm install.
import { limits } from './templates/spending-limits.mjs';
import { DEFAULT_FREE_MODELS, FREE_DEFAULTS } from '../singit-ask/src/free.mjs';

export const tasks = [
  ['Chat completion (OpenAI format)', 'POST', ['/v1/chat/completions']],
  ['Anthropic Messages', 'POST', ['/v1/messages']],
  ['OpenAI Responses', 'POST', ['/v1/responses']],
  ['Per-model chat routes', 'POST', ['/v1/chat/{model}']],
  ['Image generation', 'POST', ['/v1/images/generations']],
  ['Speech', 'POST', ['/v1/audio/speech']],
  ['Exa web search', 'POST', ['/v1/exa/search']],
  ['Exa page contents', 'POST', ['/v1/exa/contents']],
  ['Exa cited answer', 'POST', ['/v1/exa/answer']],
  ['Free models (free/ prefix)', 'POST', ['/v1/chat/completions'], true],
  ['Crypto / metal / FX prices (free)', 'GET', ['/v1/prices', '/v1/prices/crypto/{symbol}', '/v1/prices/metal/{symbol}', '/v1/prices/fx/{base}/{quote}']],
  ['Models / pricing lookups (free)', 'GET', ['/v1/models', '/pricing.json']],
  ['Health', 'GET', ['/health']],
];
const label = s => String(s).replace(/[\[\]\\|]/g, '\\$&').replace(/[\r\n]/g, ' ');
export function replaceBlock(text, name, content) {
  const start = `<!-- generated:${name}:start -->`, end = `<!-- generated:${name}:end -->`;
  const a = text.indexOf(start), b = text.indexOf(end);
  if (a < 0 || b < a || text.indexOf(start, a + 1) >= 0 || text.indexOf(end, b + 1) >= 0) throw new Error(`Malformed ${name} markers`);
  return text.slice(0, a) + `${start}\n${content}\n${end}` + text.slice(b + end.length);
}
export function taskRows(routes, format) {
  return tasks.map(([task, method, paths, free]) => {
    const entries = paths.flatMap(p => p === '/v1/chat/{model}'
      ? routes.filter(r => r.method === method && r.path.startsWith('/v1/chat/') && r.path !== '/v1/chat/completions')
      : [routes.find(r => r.method === method && r.path === p)]);
    if (!entries.length || entries.some(r => !r?.price)) throw new Error(`Missing priced route: ${task}`);
    const min = Math.min(...entries.map(r => Number(r.price.min)));
    const max = Math.max(...entries.map(r => Number(r.price.max)));
    if (![min, max].every(v => Number.isFinite(v) && v >= 0) || min > max || entries.some(r => !['USD', 'USDC'].includes(r.price.currency))) throw new Error(`Invalid route price: ${task}`);
    return [task, paths.map(p => `${method} https://ask.singitai.app${p}`).join('; '), free ? 'Free (limited daily allowance)' : max === 0 ? 'Free' : `${format(min)}..${format(max)} USDC per call`];
  });
}
export function renderLlms(text, s, routes, format) {
  const rows = taskRows(routes.routes, format);
  const table = '## I need to... -> Call -> Price\n\n| Task | Call | Price |\n| --- | --- | --- |\n' + rows.map(r => `| ${r.join(' | ')} |`).join('\n');
  const money = renderMoney(s, format);
  text = replaceBlock(text, 'ask-guide', table + '\n\n' + money);
  text = replaceBlock(text, 'model-pages', renderModelLinks());
  const models = renderModelList(s, format);
  const body = text.replace(/^<!-- generated:[^\n]+ -->\n/gm, '');
  return new Map([['llms.txt', text], ['llms-models.txt', models], ['llms-full.txt', body.trimEnd() + '\n\n' + models.replace(/^# SingIt model pages/, '## Model pages with prices')]]);
}

export function renderModelList(s, format) {
  return '# SingIt model pages\n\nsingit/auto: POST /v1/chat/completions picks eco/standard/premium by simple rules for text prompts. Optional min_tier sets a minimum. X-SingIt-Model and X-SingIt-Tier report the choice. If the chosen model\'s seller refuses before payment, the router picks the next model of the tier or the next tier up, never below min_tier; after payment it retries once with another model of the same tier only if it costs no more than the price quoted, otherwise the request fails with 503 and no charge; X-SingIt-Model and X-SingIt-Tier show what actually answered. Prices depend on the chosen model; use the live 402 quote.\n\n> Generated from models-snapshot.json; USD per 1M tokens includes markup. Settlement fees are additional. Reference request prices are USDC per call; live 402 offers are authoritative.\n\n' +
    '| Model page | Input / 1M | Output / 1M | Base upto | Base exact | Solana upto | Solana exact |\n| --- | --- | --- | --- | --- | --- | --- |\n' +
    s.models.map(m => `| [${label(m.name ?? m.id)}](https://singitai.app/models/${m.id}/) | ${m.singitUsdPerMillionTokens ? '$' + format(m.singitUsdPerMillionTokens.input) : 'Not listed'} | ${m.singitUsdPerMillionTokens ? '$' + format(m.singitUsdPerMillionTokens.output) : 'Not listed'} | ${['base', 'solana'].flatMap(n => ['upto', 'exact'].map(scheme => format(m[scheme][n].usd))).join(' | ')} |`).join('\n') + '\n';
}

export function renderModelLinks() {
  const links = [
    ['Privacy policy', 'https://singitai.app/privacy/'],
    ['Data deletion', 'https://singitai.app/data-deletion/'],
    ['Model pages with prices', 'https://singitai.app/llms-models.txt'],
    ['All model pages', 'https://singitai.app/models/all/'],
    ['Live models', 'https://ask.singitai.app/v1/models'],
    ['Live reference prices', 'https://ask.singitai.app/pricing.json'],
    ...limits.map(p => [p.title.replace(' | SingIt', ''), `https://singitai.app/${p.slug}/`]),
  ];
  return '## Models\n\n' + links.map(([name, url]) => `- [${label(name)}](${url})`).join('\n');
}

export function renderMoney(s, format) {
  const fees = ['base', 'solana'].map(n => format(s.fees.perAnswerUsd[s.networks[n].chain]));
  return `## Money rules for agents

Call without payment: HTTP 402 includes PAYMENT-REQUIRED (base64 JSON, x402Version 2). Decode that header, sign one offer, and retry the same request with PAYMENT-SIGNATURE. Success is HTTP 200 with PAYMENT-RESPONSE.

\`upto\` is listed first: pay the actual cost up to the signed ceiling. \`exact\` is a flat price for a typical-length answer (about 30% of max_tokens), settled at that flat price whatever length the answer turns out to be. Do not sign above your budget.

LLM, image and speech charges are model cost plus ${format(s.fees.markupPct)}% markup, plus a settlement fee of $${fees[0]} on Base or $${fees[1]} on Solana. Solana uses the same API URLs as Base. Exa uses Exa's own price with no markup or settlement fee by default. The route table shows discovery ranges in USDC; the request's 402 offer is authoritative.

Charged only when a billable answer came back: an upstream 503 never charges. Do not retry a 400/413/422 on another model; fix the request. A 409 means this payment signature was already used. For 503, wait for Retry-After where supplied. A 429 on the free tier means wait for Retry-After.

Free tier: ${DEFAULT_FREE_MODELS.length} free models, up to ${FREE_DEFAULTS.perVisitorPerDay} answers a day per network, with a shared daily cost cap of $${FREE_DEFAULTS.budgetUsdPerDay}. The shared free tier may run out for the day (free_budget_exhausted); it resets at midnight UTC. No wallet or key required.

Reference chat request: ${s.referenceRequest.messages} message(s), ${s.referenceRequest.chars} characters, ${s.referenceRequest.inputTokens} input tokens, max_tokens ${s.referenceRequest.maxOutputTokens}. Per-model reference prices are in the full models list, with upto before exact.`;
}
