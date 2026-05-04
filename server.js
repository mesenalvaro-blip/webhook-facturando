require('dotenv').config();
const express = require('express');
const axios = require('axios');

const app = express();
app.use(express.json());

const ODOO_URL = process.env.ODOO_URL || 'https://happy-value-costa-rica.odoo.com';
const ODOO_DB = process.env.ODOO_DB;
const ODOO_USER = process.env.ODOO_USER;
const ODOO_PASSWORD = process.env.ODOO_PASSWORD;

const FACTURANDO_URL =
  'https://sqjsnwfhimttilyapwmu.supabase.co/functions/v1/api-webhook/webhook/560c4a8c9ac376b1ff07b05f15b36138fc3b36cc13592f7f';

// In-memory log of the last 20 requests for debugging
const recentRequests = [];
function logRequest(entry) {
  recentRequests.unshift({ ts: new Date().toISOString(), ...entry });
  if (recentRequests.length > 20) recentRequests.pop();
}

let cachedUid = null;

async function getOdooUid() {
  if (cachedUid) return cachedUid;

  const res = await axios.post(`${ODOO_URL}/jsonrpc`, {
    jsonrpc: '2.0',
    method: 'call',
    id: 1,
    params: {
      service: 'common',
      method: 'authenticate',
      args: [ODOO_DB, ODOO_USER, ODOO_PASSWORD, {}],
    },
  });

  if (!res.data.result) {
    throw new Error('Odoo authentication failed — check ODOO_DB, ODOO_USER and ODOO_PASSWORD');
  }

  cachedUid = res.data.result;
  return cachedUid;
}

async function odooRead(model, ids, fields) {
  if (!ids || ids.length === 0) return [];

  const uid = await getOdooUid();

  const res = await axios.post(`${ODOO_URL}/jsonrpc`, {
    jsonrpc: '2.0',
    method: 'call',
    id: 1,
    params: {
      service: 'object',
      method: 'execute_kw',
      args: [ODOO_DB, uid, ODOO_PASSWORD, model, 'read', [ids], { fields }],
    },
  });

  if (res.data.error) {
    throw new Error(`Odoo error on ${model}.read: ${JSON.stringify(res.data.error)}`);
  }

  return res.data.result;
}

// Like odooRead but silently retries without optional fields if Odoo rejects them.
async function odooReadSafe(model, ids, requiredFields, optionalFields = []) {
  if (!ids || ids.length === 0) return [];

  try {
    return await odooRead(model, ids, [...requiredFields, ...optionalFields]);
  } catch (err) {
    if (!optionalFields.length || !err.message.includes('Invalid field')) throw err;
    console.warn(`[odoo] optional fields unavailable on ${model}, retrying without them`);
    return await odooRead(model, ids, requiredFields);
  }
}

// ---------------------------------------------------------------------------
// Main webhook endpoint
// ---------------------------------------------------------------------------
app.post('/webhook-facturando', async (req, res) => {
  const payload = req.body;
  const received = JSON.stringify(payload);
  console.log('[webhook] received:', received);

  try {
    // Ignore non-posted or non-customer-invoice events
    if (payload.state !== 'posted' || payload.move_type !== 'out_invoice') {
      const msg = `ignored: state=${payload.state} move_type=${payload.move_type}`;
      console.log('[webhook]', msg);
      logRequest({ payload, result: 'ignored', reason: msg });
      return res.json({ ok: true, message: msg });
    }

    const moveId = payload.id || payload._id;
    if (!moveId) {
      throw new Error('No move id found in payload (expected id or _id)');
    }

    // 1. Read the move for invoice number, partner, currency and line IDs
    const [move] = await odooRead('account.move', [moveId], [
      'name', 'partner_id', 'currency_id', 'invoice_line_ids',
    ]);

    const invoiceNumber = move.name;

    // partner_id can be false | number | [id, "name"]
    const rawPartner = payload.partner_id;
    const partnerId =
      rawPartner && rawPartner !== false
        ? typeof rawPartner === 'object'
          ? rawPartner[0] ?? rawPartner.id
          : rawPartner
        : Array.isArray(move.partner_id)
        ? move.partner_id[0]
        : move.partner_id || null;

    const currencyId =
      payload.currency_id ||
      (Array.isArray(move.currency_id) ? move.currency_id[0] : move.currency_id) ||
      null;

    const lineIds =
      payload.invoice_line_ids && payload.invoice_line_ids.length > 0
        ? payload.invoice_line_ids
        : move.invoice_line_ids;

    // 2. Read partner
    let partner = { name: 'Sin nombre', vat: '', l10n_cr_identification_type: '02' };
    if (partnerId) {
      const rows = await odooReadSafe(
        'res.partner', [partnerId],
        ['name', 'vat'],
        ['l10n_cr_identification_type']
      );
      if (rows.length) partner = rows[0];
    }

    // 3. Read currency
    let moneda = 'CRC';
    if (currencyId) {
      const rows = await odooRead('res.currency', [currencyId], ['name']);
      if (rows.length) moneda = rows[0].name;
    }

    // 4. Read invoice lines — also fetch product_id so we can look up CABYS from the product
    const allLines = await odooReadSafe(
      'account.move.line', lineIds,
      ['name', 'quantity', 'price_unit', 'tax_ids', 'display_type', 'product_id'],
      ['l10n_cr_cabys_code']
    );

    // Skip section/note lines and lines with no description (e.g. price-0 placeholder rows)
    const SKIP_TYPES = ['line_section', 'line_note'];
    const lines = allLines.filter(
      (l) => !SKIP_TYPES.includes(l.display_type) && l.name && l.name.trim() !== ''
    );

    if (lines.length === 0) {
      throw new Error(`Move ${moveId}: no valid product lines found (${allLines.length} total, all are section/note or have no description)`);
    }

    // 5. Resolve taxes
    const allTaxIds = [...new Set(lines.flatMap((l) => l.tax_ids || []))];
    const taxMap = {};
    if (allTaxIds.length > 0) {
      const taxes = await odooRead('account.tax', allTaxIds, ['amount']);
      taxes.forEach((t) => { taxMap[t.id] = t.amount; });
    }

    // 5b. Resolve CABYS from product.template (field x_cabys_code in this Odoo instance)
    const cabysMap = {};
    const linesWithProduct = lines.filter((l) => l.product_id);
    if (linesWithProduct.length > 0) {
      const productIds = [...new Set(linesWithProduct.map((l) =>
        Array.isArray(l.product_id) ? l.product_id[0] : l.product_id
      ))];
      // product.product has product_tmpl_id pointing to the template
      const products = await odooRead('product.product', productIds, ['product_tmpl_id']);
      const tmplIds = [...new Set(products.map((p) =>
        Array.isArray(p.product_tmpl_id) ? p.product_tmpl_id[0] : p.product_tmpl_id
      ))];
      // x_cabys_code = custom CABYS field; hs_code = "Código SA" (used as fallback)
      const templates = await odooReadSafe('product.template', tmplIds, ['hs_code'], ['x_cabys_code', 'l10n_cr_cabys_code']);
      const tmplMap = {};
      templates.forEach((t) => {
        tmplMap[t.id] = t.x_cabys_code || t.l10n_cr_cabys_code || t.hs_code || '';
      });
      products.forEach((p) => {
        const tmplId = Array.isArray(p.product_tmpl_id) ? p.product_tmpl_id[0] : p.product_tmpl_id;
        if (tmplMap[tmplId]) cabysMap[p.id] = tmplMap[tmplId];
      });
    }
    console.log('[webhook] cabysMap:', JSON.stringify(cabysMap));

    // 6. Build FacturAndo payload
    const facturandoPayload = {
      customer_name: partner.name,
      cliente_tipo_id: partner.l10n_cr_identification_type || '02',
      cliente_cedula: (partner.vat || '').replace(/\D/g, ''),
      moneda,
      numero_factura: invoiceNumber,
      lines: lines.map((line) => {
        const productId = Array.isArray(line.product_id) ? line.product_id[0] : line.product_id;
        const cabys = line.l10n_cr_cabys_code || cabysMap[productId] || '0000000000000';
        return ({
        description: line.name,
        quantity: line.quantity,
        unit_price: line.price_unit,
        cabys_code: cabys,
        tasa_impuestos:
          line.tax_ids && line.tax_ids.length > 0
            ? taxMap[line.tax_ids[0]] ?? 13
            : 0,
        });
      }),
    };

    console.log('[webhook] sending to FacturAndo:', JSON.stringify(facturandoPayload));

    // 7. Forward to FacturAndo
    const facturandoRes = await axios.post(FACTURANDO_URL, facturandoPayload, {
      headers: { 'Content-Type': 'application/json' },
      timeout: 15000,
    });

    console.log('[webhook] FacturAndo response:', facturandoRes.status, JSON.stringify(facturandoRes.data));

    logRequest({ moveId, invoiceNumber, result: 'ok', sent: facturandoPayload, facturando: facturandoRes.data });
    return res.json({ ok: true, facturando: facturandoRes.data });

  } catch (err) {
    console.error('[webhook] error:', err.message);
    if (err.response) {
      console.error('[webhook] upstream:', err.response.status, JSON.stringify(err.response.data));
    }
    if (err.message.includes('authentication')) cachedUid = null;

    const detail = err.response ? { status: err.response.status, body: err.response.data } : null;
    logRequest({ payload, result: 'error', error: err.message, upstream: detail });
    return res.status(500).json({ ok: false, error: err.message, upstream: detail });
  }
});

// Manually trigger processing for an invoice by number, e.g.:
//   POST /trigger  { "numero": "INV/2026/00060" }
app.post('/trigger', async (req, res) => {
  const { numero } = req.body || {};
  if (!numero) return res.status(400).json({ error: 'Provide { "numero": "INV/2026/XXXXX" }' });

  try {
    const uid = await getOdooUid();
    const searchRes = await axios.post(`${ODOO_URL}/jsonrpc`, {
      jsonrpc: '2.0', method: 'call', id: 1,
      params: {
        service: 'object', method: 'execute_kw',
        args: [ODOO_DB, uid, ODOO_PASSWORD, 'account.move', 'search_read',
          [[['name', '=', numero], ['move_type', '=', 'out_invoice']]],
          { fields: ['id', 'name', 'state'], limit: 1 }
        ],
      },
    });

    if (searchRes.data.error) throw new Error(JSON.stringify(searchRes.data.error));
    const results = searchRes.data.result;
    if (!results || results.length === 0) return res.status(404).json({ error: `Invoice ${numero} not found in Odoo` });

    const move = results[0];
    // Reuse the main webhook handler by doing an internal POST-style call
    const fakePayload = {
      id: move.id,
      _id: move.id,
      move_type: 'out_invoice',
      state: move.state,
    };

    // Forward to our own webhook handler logic directly
    const webhookRes = await axios.post(
      `http://localhost:${process.env.PORT || 3000}/webhook-facturando`,
      fakePayload,
      { headers: { 'Content-Type': 'application/json' } }
    );

    return res.json({ triggered: numero, move_id: move.id, result: webhookRes.data });
  } catch (err) {
    return res.status(500).json({ error: err.message });
  }
});

// Set CABYS code on a product by its internal reference
//   POST /set-cabys  { "referencia": "HV-SP-003", "cabys": "2392100000100" }
//   POST /set-cabys  { "productos": [{ "referencia": "HV-SP-003", "cabys": "..." }, ...] }
app.post('/set-cabys', async (req, res) => {
  try {
    const uid = await getOdooUid();
    const items = req.body.productos || (req.body.referencia ? [req.body] : []);
    if (!items.length) return res.status(400).json({ error: 'Provide referencia+cabys or productos array' });

    const results = [];
    for (const { referencia, cabys } of items) {
      // Find template by default_code (internal reference)
      const searchRes = await axios.post(`${ODOO_URL}/jsonrpc`, {
        jsonrpc: '2.0', method: 'call', id: 1,
        params: {
          service: 'object', method: 'execute_kw',
          args: [ODOO_DB, uid, ODOO_PASSWORD, 'product.template', 'search_read',
            [[['default_code', '=', referencia]]],
            { fields: ['id', 'name', 'default_code'], limit: 1 }
          ],
        },
      });
      const found = searchRes.data.result;
      if (!found || !found.length) { results.push({ referencia, error: 'not found' }); continue; }

      const tmplId = found[0].id;
      const writeRes = await axios.post(`${ODOO_URL}/jsonrpc`, {
        jsonrpc: '2.0', method: 'call', id: 1,
        params: {
          service: 'object', method: 'execute_kw',
          args: [ODOO_DB, uid, ODOO_PASSWORD, 'product.template', 'write',
            [[tmplId], { x_cabys_code: cabys }]
          ],
        },
      });
      if (writeRes.data.error) throw new Error(JSON.stringify(writeRes.data.error));
      results.push({ referencia, producto: found[0].name, cabys, ok: true });
    }
    return res.json({ results });
  } catch (err) {
    return res.status(500).json({ error: err.message });
  }
});

// Returns all fields on a model that contain a keyword — e.g. /odoo-fields?model=product.template&q=cabys
app.get('/odoo-fields', async (req, res) => {
  const model = req.query.model || 'product.template';
  const q = (req.query.q || 'cabys').toLowerCase();
  try {
    const uid = await getOdooUid();
    const r = await axios.post(`${ODOO_URL}/jsonrpc`, {
      jsonrpc: '2.0', method: 'call', id: 1,
      params: {
        service: 'object', method: 'execute_kw',
        args: [ODOO_DB, uid, ODOO_PASSWORD, model, 'fields_get', [], { attributes: ['string', 'type'] }],
      },
    });
    if (r.data.error) throw new Error(JSON.stringify(r.data.error));
    const matches = Object.entries(r.data.result)
      .filter(([k, v]) => k.toLowerCase().includes(q) || (v.string || '').toLowerCase().includes(q))
      .reduce((acc, [k, v]) => { acc[k] = v; return acc; }, {});
    return res.json({ model, query: q, matches });
  } catch (err) {
    return res.status(500).json({ error: err.message });
  }
});

// Shows the last 20 requests — useful for diagnosing Odoo webhook issues
app.get('/debug', (_req, res) => res.json(recentRequests));

app.get('/health', (_req, res) => res.json({ status: 'ok' }));

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`Webhook server listening on port ${PORT}`));
