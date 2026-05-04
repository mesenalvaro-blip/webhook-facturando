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

// Cache the Odoo UID so we don't re-authenticate on every request
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

// Generic read helper — throws on Odoo-level errors
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

// Like odooRead but silently drops fields that Odoo rejects as invalid.
// Useful for optional localisation fields (l10n_cr_*) that may not be installed.
async function odooReadSafe(model, ids, requiredFields, optionalFields = []) {
  if (!ids || ids.length === 0) return [];

  try {
    return await odooRead(model, ids, [...requiredFields, ...optionalFields]);
  } catch (err) {
    if (!optionalFields.length || !err.message.includes('Invalid field')) throw err;
    console.warn(`[odoo] optional fields not available on ${model}, retrying without them`);
    return await odooRead(model, ids, requiredFields);
  }
}

// ---------------------------------------------------------------------------
// Main webhook endpoint
// ---------------------------------------------------------------------------
app.post('/webhook-facturando', async (req, res) => {
  try {
    const payload = req.body;
    console.log('[webhook] received:', JSON.stringify(payload));

    // Only process posted customer invoices
    if (payload.state !== 'posted' || payload.move_type !== 'out_invoice') {
      console.log('[webhook] ignored — state or move_type not applicable');
      return res.json({ ok: true, message: 'ignored: not a posted out_invoice' });
    }

    const moveId = payload.id || payload._id;

    // 1. Read the move — always needed for the invoice number; also fills in
    //    partner_id / currency_id / invoice_line_ids when Odoo sent only IDs
    const [move] = await odooRead('account.move', [moveId], [
      'name',
      'partner_id',
      'currency_id',
      'invoice_line_ids',
    ]);

    const invoiceNumber = move.name;

    // partner_id arrives as false, a plain number, or [id, name] from Odoo
    const rawPartner = payload.partner_id;
    const partnerId =
      rawPartner && rawPartner !== false
        ? typeof rawPartner === 'object'
          ? rawPartner[0] ?? rawPartner.id
          : rawPartner
        : move.partner_id
        ? move.partner_id[0]
        : null;

    // currency_id and line IDs — prefer payload, fall back to move
    const currencyId =
      payload.currency_id ||
      (move.currency_id ? move.currency_id[0] : null);

    const lineIds =
      payload.invoice_line_ids && payload.invoice_line_ids.length > 0
        ? payload.invoice_line_ids
        : move.invoice_line_ids;

    // 2. Read partner details — l10n_cr_identification_type is optional (requires CR localisation)
    let partner = { name: 'Sin nombre', vat: '', l10n_cr_identification_type: '02' };
    if (partnerId) {
      const rows = await odooReadSafe(
        'res.partner',
        [partnerId],
        ['name', 'vat'],
        ['l10n_cr_identification_type']
      );
      if (rows.length) partner = rows[0];
    }

    // 3. Read currency name (CRC, USD, …)
    let moneda = 'CRC';
    if (currencyId) {
      const rows = await odooRead('res.currency', [currencyId], ['name']);
      if (rows.length) moneda = rows[0].name;
    }

    // 4. Read invoice lines — l10n_cr_cabys_code is optional (requires CR localisation)
    const lines = await odooReadSafe(
      'account.move.line',
      lineIds,
      ['name', 'quantity', 'price_unit', 'tax_ids'],
      ['l10n_cr_cabys_code']
    );

    // 5. Resolve taxes — build a map { taxId -> amount% }
    const allTaxIds = [...new Set(lines.flatMap((l) => l.tax_ids || []))];
    const taxMap = {};
    if (allTaxIds.length > 0) {
      const taxes = await odooRead('account.tax', allTaxIds, ['amount']);
      taxes.forEach((t) => {
        taxMap[t.id] = t.amount;
      });
    }

    // 6. Build FacturAndo payload
    const facturandoPayload = {
      cliente_nombre: partner.name,
      cliente_tipo_id: partner.l10n_cr_identification_type || '02',
      cliente_cedula: (partner.vat || '').replace(/\D/g, ''),
      moneda,
      numero_factura: invoiceNumber,
      lineas: lines.map((line) => ({
        descripcion: line.name,
        cantidad: line.quantity,
        unit_price: line.price_unit,
        cabys_code: line.l10n_cr_cabys_code || '',
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

    return res.json({ ok: true, facturando: facturandoRes.data });
  } catch (err) {
    console.error('[webhook] error:', err.message);
    if (err.response) {
      console.error('[webhook] upstream response:', err.response.status, JSON.stringify(err.response.data));
    }
    if (err.message.includes('authentication')) cachedUid = null;
    const detail = err.response ? { status: err.response.status, body: err.response.data } : null;
    return res.status(500).json({ ok: false, error: err.message, upstream: detail });
  }
});

// Health check — Render uses this to confirm the service is up
app.get('/health', (_req, res) => res.json({ status: 'ok' }));

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`Webhook server listening on port ${PORT}`));
