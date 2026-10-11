// Mock bank for the final case, hosted on Cloudflare Workers.
// Generic skeleton: POST /v1/<tool> for the seven bank tools, customers from customers.json.
// After the case opens, field names/rules are aligned to the case's service contract and
// customers.json is generated from its test customers (mock/make_customers.py).
import CUSTOMERS from "../customers.json";

const IDLE_RESET_MS = 3 * 60 * 1000; // a new call after a pause starts with fresh state
const sessions = new Map();          // customer_id -> per-call state (per isolate)

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json; charset=utf-8" } });
}

function customer(id) {
  return CUSTOMERS.customers.find((c) => String(c.customer_id) === String(id));
}

function session(id, fresh = false) {
  const now = Date.now();
  let s = sessions.get(id);
  if (fresh || !s || s.closed || now - s.last > IDLE_RESET_MS) {
    s = { verified: new Set(), wrongLeft: CUSTOMERS.wrong_answer_limit ?? 3, closed: false,
          ledger: { promises: [], notes: [], callbacks: [], transfers: [], outcome: null, reason: null } };
    sessions.set(id, s);
  }
  s.last = now;
  return s;
}

function same(a, b) {
  const norm = (v) => String(v ?? "").trim().toLowerCase().replace(/\s+/g, " ");
  return norm(a) === norm(b);
}

const handlers = {
  verify_identity(c, s, body) {
    const field = body.field, expected = c.identity?.[field];
    if (expected === undefined) return [{ ok: false, error: "unknown identity field" }, 400];
    if (s.wrongLeft <= 0) return [{ ok: false, error: "identity check exhausted" }, 403];
    const passed = same(body.answer, expected);
    if (passed) s.verified.add(field); else s.wrongLeft -= 1;
    const all = Object.keys(c.identity).every((f) => s.verified.has(f));
    return [{ ok: true, verified: passed, all_verified: all, wrong_answers_left: s.wrongLeft }];
  },
  get_account(c, s) {
    if (!Object.keys(c.identity).every((f) => s.verified.has(f))) return [{ ok: false, error: "identity not verified" }, 403];
    return [{ ok: true, ...c.account }];
  },
  create_promise(c, s, body) {
    if (c.faults?.create_promise && (s.promiseFailures = (s.promiseFailures || 0) + 1) <= c.faults.create_promise)
      return [{ ok: false, error: "service unavailable; nothing was recorded" }, 500];
    s.ledger.promises.push(body);
    return [{ ok: true, promise_id: "PR" + s.ledger.promises.length }];
  },
  schedule_callback(c, s, body) { s.ledger.callbacks.push(body); return [{ ok: true }]; },
  transfer_to_agent(c, s, body) { s.ledger.transfers.push(body); return [{ ok: true }]; },
  add_note(c, s, body) { s.ledger.notes.push(body); return [{ ok: true }]; },
  close_call(c, s, body) {
    s.ledger.outcome = body.outcome ?? null; s.ledger.reason = body.reason ?? null; s.closed = true;
    return [{ ok: true }];
  },
};

export default {
  async fetch(request) {
    const url = new URL(request.url);
    if (request.method === "GET") return json({ ok: true, service: "mock bank", customers: CUSTOMERS.customers.length });
    const tool = url.pathname.split("/").filter(Boolean).pop();
    if (request.method !== "POST" || !handlers[tool]) return json({ ok: false, error: "unknown route" }, 404);
    let body;
    try { body = await request.json(); } catch { return json({ ok: false, error: "invalid JSON body" }, 400); }
    const id = body.customer_id;
    const c = customer(id);
    if (!c) return json({ ok: false, error: "unknown customer" }, 404);
    // The first identity check of a call starts fresh state for that customer.
    const s = session(id, tool === "verify_identity" && (sessions.get(id)?.closed ?? false));
    const [result, status = 200] = handlers[tool](c, s, body);
    return json(result, status);
  },
};
