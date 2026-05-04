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

    // 4. Read invoice lines — include display_type to filter out section/note rows
    const allLines = await odooReadSafe(
      'account.move.line', lineIds,
      ['name', 'quantity', 'price_unit', 'tax_ids', 'display_type'],
      ['l10n_cr_cabys_code']
    );

    // Only keep actual product lines (display_type is false/empty for product lines)
    const lines = allLines.filter((l) => !l.display_type);

    if (lines.length === 0) {
      throw new Error(`Move ${moveId} has no product lines (all lines are section/note or empty)`);
    }

    // 5. Resolve taxes
    const allTaxIds = [...new Set(lines.flatMap((l) => l.tax_ids || []))];
    const taxMap = {};
    if (allTaxIds.length > 0) {
      const taxes = await odooRead('account.tax', allTaxIds, ['amount']);
      taxes.forEach((t) => { taxMap[t.id] = t.amount; });
    }

    // 6. Build FacturAndo payload
    const facturandoPayload = {
      customer_name: partner.name,
      cliente_tipo_id: partner.l10n_cr_identification_type || '02',
      cliente_cedula: (partner.vat || '').replace(/\D/g, ''),
      moneda,
      numero_factura: invoiceNumber,
      lines: lines.map((line) => ({
        description: line.name,
        quantity: line.quantity,
        unit_price: line.price_unit,
        cabys_code: line.l10n_cr_cabys_code || '0000000000000',
        tasa_impuestos:
          line.tax_ids && line.tax_ids.length > 0
            ? taxMap[line.tax_ids[0]] ?? 13
            : 0,
      })),
    };

    console.log('[webhook] sending to FacturAndo:', JSON.stringify(facturandoPayload));

    // 7. Forward to FacturAndo
    const facturandoRes = await axios.post(FACTURANDO_URL, facturandoPayload, {
      headers: { 'Content-Type': 'application/json' },
      timeout: 15000,
    });

    console.log('[webhook] FacturAndo response:', facturandoRes.status, JSON.stringify(facturandoRes.data));

    logRequest({ moveId, invoiceNumber, result: 'ok', facturando: facturandoRes.data });
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

// Shows the last 20 requests — useful for diagnosing Odoo webhook issues
app.get('/debug', (_req, res) => res.json(recentRequests));

app.get('/health', (_req, res) => res.json({ status: 'ok' }));

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`Webhook server listening on port ${PORT}`));
