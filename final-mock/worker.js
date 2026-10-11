// Freya Bank statement/debt services: mock of the bank's voice API (final case).
// Serves exactly the answers in test-customers.json (status code and body), per the
// service contract (sources/documents/2026-10-01-service-contract.md) and
// test-environment/test-customers.md. Not part of the builder.
import DB from "./test-customers.json";

const SERVICES = ["cards.list", "cards.balance", "statement.week.get", "statement.week.decide",
                  "statement.week.change", "contact.email.get", "statement.pdf.send"];

// Required data fields per service (empty or missing -> 400 INVALID_ARGUMENTS_ERROR).
const REQUIRED = {
  "cards.list": ["customerNo"],
  "cards.balance": ["cardNumber"],
  "statement.week.get": ["cardNumber"],
  "statement.week.decide": ["cardNumber", "newStatementWeek", "statementDay"],
  "statement.week.change": ["cardNumber", "newStatementWeek", "statementDay"],
  "contact.email.get": ["customerNo", "intAddressInfoType"],
  "statement.pdf.send": ["customerNo", "cardNumber", "currencyType", "statementDate", "emailAddress"],
};
const FIELD_TEXT = { cardNumber: "card number", customerNo: "customer number" };

// Not-found answers from contract section 4; services without a 404 answer 400.
const NOT_FOUND = {
  "cards.list": [404, "FB_IVR_CHANNEL_102", "WARNING", "Müşteriye ait geçerli kart bilgisi bulunamadı.", "NotFoundException"],
  "cards.balance": [404, "FB_IVR_CHANNEL_120", "WARNING", "Kart bakiye servisi hatası: kart bulunamadı", "NotFoundException"],
  "statement.week.get": [404, "FB_IVR_CHANNEL_121", "WARNING", "Ekstre haftası servisi hatası: kart bulunamadı", "NotFoundException"],
  "statement.pdf.send": [404, "FB_IVR_CHANNEL_107", "WARNING", "Pdf dokümanı bulunamadı",
                         "com.freyabank.ivrgateway.ivrchannel.rest.exception.NotFoundException"],
};

const customers = new Map(DB.customers.map((c) => [String(c.customerNo).trim(), c]));
const byCard = new Map();
for (const c of DB.customers) {
  for (const service of ["cards.balance", "statement.week.get", "statement.week.decide", "statement.week.change"]) {
    for (const card of Object.keys(c.services?.[service] || {})) byCard.set(card, c);
  }
}

function guid() {
  return crypto.randomUUID().replace(/-/g, "");
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json; charset=utf-8" } });
}

function error(status, code, type, message, rootCause) {
  const id = guid();
  return json({ result: { guid: id, returnCode: code, messageText: message, code, message, type,
                          details: { guid: id, rootCause, errorOrigin: "ivr-gateway-test:ivr-channel-v0" } } }, status);
}

function invalid(message) {
  return error(400, "INVALID_ARGUMENTS_ERROR", "ERROR", message, "InvalidArgumentsException");
}

function notFound(service) {
  const entry = NOT_FOUND[service];
  return entry ? error(...entry) : invalid("no record for this request");
}

async function answer(entry) {
  if (!entry || typeof entry.http !== "number") return null;
  if (entry.delaySeconds) await new Promise((r) => setTimeout(r, entry.delaySeconds * 1000));
  return json(entry.response, entry.http);
}

const isTemplate = (v) => typeof v === "string" && v.includes("{{");
const empty = (v) => v === undefined || v === null || String(v).trim() === "";

async function handle(service, data) {
  for (const field of REQUIRED[service]) {
    if (empty(data[field])) return invalid(`${FIELD_TEXT[field] || field} cannot be empty`);
  }
  // Freya's "Test API Request" may send a literal {{variable}}: answer with the first record.
  let customerNo = String(data.customerNo ?? "").trim();
  if (isTemplate(customerNo)) customerNo = String(DB.customers[0].customerNo);
  let card = String(data.cardNumber ?? "").trim();
  if (isTemplate(card)) card = byCard.keys().next().value;
  const week = String(data.newStatementWeek ?? "").trim();

  if (["cards.list", "contact.email.get", "statement.pdf.send"].includes(service)) {
    const customer = customers.get(customerNo);
    const entry = customer?.services?.[service];
    return (await answer(entry)) || notFound(service);
  }
  const customer = byCard.get(card);
  let entry = customer?.services?.[service]?.[card];
  if (service === "statement.week.decide" || service === "statement.week.change") entry = entry?.[week];
  return (await answer(entry)) || notFound(service);
}

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "");
    if (request.method === "GET" && (path === "" || path === "/health")) {
      return json({ ok: true, service: "Freya Bank voice API mock", customers: DB.customers.length });
    }
    if (request.method === "POST" && path.endsWith("/oauth2/token")) {
      // The test environment does not check tokens; answer like the gateway would.
      return json({ access_token: "test-environment-token", token_type: "Bearer", expires_in: 1800 });
    }
    const service = path.split("/").pop();
    if (request.method !== "POST" || !SERVICES.includes(service)) return json({ error: "not found" }, 404);
    let body;
    try { body = await request.json(); } catch { return invalid("request body must be JSON"); }
    const data = body && typeof body.data === "object" && body.data !== null ? body.data : {};
    return handle(service, data);
  },
};
