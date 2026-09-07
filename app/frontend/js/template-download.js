/**
 * Netgravity — Upload Template Builder
 * ====================================
 * Produces the workbook behind "Download template" on the upload screen.
 *
 * The sheet names and column headers come from
 * `GET /api/ingestions/preview/schema`, which the server generates from the
 * extractor's own `_COLUMN_ROLES` table. Nothing about the schema is written
 * down here — a template that listed columns of its own would drift from the
 * parser the moment either changed, and this codebase already has the scar
 * from that arrangement: a "mapped to" dropdown with nine hand-typed options
 * that did not include what the server sent, so every row rendered as the
 * first one.
 *
 * The file is a genuine .xlsx, not a CSV renamed: one sheet per table, so it
 * can be filled in and uploaded straight back. It is assembled here rather
 * than fetched because .xlsx is a ZIP of XML parts and both are small enough
 * to write directly — the alternative is a spreadsheet library from a CDN,
 * which the Content Security Policy blocks, or a new server-side dependency.
 */

/* ─── CRC-32, for the ZIP entries ─────────────────────────────── */
let CRC_TABLE = null;

function crcTable() {
  if (CRC_TABLE) return CRC_TABLE;
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) {
      c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1;
    }
    table[n] = c >>> 0;
  }
  CRC_TABLE = table;
  return table;
}

function crc32(bytes) {
  const table = crcTable();
  let crc = 0xFFFFFFFF;
  for (let i = 0; i < bytes.length; i += 1) {
    crc = table[(crc ^ bytes[i]) & 0xFF] ^ (crc >>> 8);
  }
  return (crc ^ 0xFFFFFFFF) >>> 0;
}

/* ─── Minimal store-only ZIP ──────────────────────────────────── */
function u16(v) { return [v & 0xFF, (v >>> 8) & 0xFF]; }
function u32(v) { return [v & 0xFF, (v >>> 8) & 0xFF, (v >>> 16) & 0xFF, (v >>> 24) & 0xFF]; }

/**
 * Build a ZIP with no compression.
 *
 * Stored entries keep this to arithmetic — a deflate implementation would be
 * the bulk of the module for a file measured in kilobytes. Every reader,
 * Excel included, accepts stored entries.
 */
function zip(entries) {
  const encoder = new TextEncoder();
  const parts = [];
  const central = [];
  let offset = 0;

  entries.forEach((entry) => {
    const nameBytes = encoder.encode(entry.name);
    const dataBytes = entry.data instanceof Uint8Array
      ? entry.data : encoder.encode(entry.data);
    const sum = crc32(dataBytes);

    const local = [
      ...u32(0x04034b50), ...u16(20), ...u16(0x0800), ...u16(0),
      ...u16(0), ...u16(0),                       // DOS time / date
      ...u32(sum), ...u32(dataBytes.length), ...u32(dataBytes.length),
      ...u16(nameBytes.length), ...u16(0),
    ];
    parts.push(new Uint8Array(local), nameBytes, dataBytes);

    central.push({
      nameBytes, sum, size: dataBytes.length, offset,
    });
    offset += local.length + nameBytes.length + dataBytes.length;
  });

  const dirParts = [];
  let dirSize = 0;
  central.forEach((c) => {
    const head = [
      ...u32(0x02014b50), ...u16(20), ...u16(20), ...u16(0x0800), ...u16(0),
      ...u16(0), ...u16(0),
      ...u32(c.sum), ...u32(c.size), ...u32(c.size),
      ...u16(c.nameBytes.length), ...u16(0), ...u16(0),
      ...u16(0), ...u16(0), ...u32(0),
      ...u32(c.offset),
    ];
    dirParts.push(new Uint8Array(head), c.nameBytes);
    dirSize += head.length + c.nameBytes.length;
  });

  const end = new Uint8Array([
    ...u32(0x06054b50), ...u16(0), ...u16(0),
    ...u16(central.length), ...u16(central.length),
    ...u32(dirSize), ...u32(offset), ...u16(0),
  ]);

  return new Blob([...parts, ...dirParts, end],
    { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' });
}

/* ─── Minimal .xlsx ───────────────────────────────────────────── */
function xmlEscape(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;' }[c]
  ));
}

/** A1, B1, … Z1, AA1, … for the header row. */
function columnRef(index) {
  let n = index + 1;
  let ref = '';
  while (n > 0) {
    const rem = (n - 1) % 26;
    ref = String.fromCharCode(65 + rem) + ref;
    n = Math.floor((n - 1) / 26);
  }
  return ref;
}

/**
 * Excel refuses a sheet name over 31 characters or containing []:*?/\.
 * The server's names are already safe; this keeps that true if they change.
 */
function safeSheetName(name, used) {
  let base = String(name || 'Sheet').replace(/[[\]:*?/\\]/g, '_').slice(0, 31) || 'Sheet';
  let candidate = base;
  let n = 2;
  while (used.has(candidate.toLowerCase())) {
    const suffix = `_${n}`;
    candidate = base.slice(0, 31 - suffix.length) + suffix;
    n += 1;
  }
  used.add(candidate.toLowerCase());
  return candidate;
}

function sheetXml(headers) {
  const cells = headers.map((h, i) =>
    `<c r="${columnRef(i)}1" t="inlineStr"><is><t xml:space="preserve">${xmlEscape(h)}</t></is></c>`
  ).join('');
  return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    + '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    + `<sheetData><row r="1">${cells}</row></sheetData></worksheet>`;
}

/**
 * A workbook with one sheet per table and the parser's own headers on row 1.
 *
 * `sheets` is the payload of `GET /api/ingestions/preview/schema`.
 */
export function buildTemplateWorkbook(sheets) {
  const used = new Set();
  const named = (sheets || []).map((s) => ({
    name: safeSheetName(s.sheet || s.role, used),
    headers: (s.columns || []).map((c) => c.header).filter(Boolean),
  })).filter((s) => s.headers.length);

  if (!named.length) throw new Error('The server returned no sheets to build from.');

  const NS_PKG = 'http://schemas.openxmlformats.org/package/2006/relationships';
  const NS_OFF = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships';

  const contentTypes = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    + '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    + '<Default Extension="xml" ContentType="application/xml"/>'
    + '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    + named.map((_, i) =>
      `<Override PartName="/xl/worksheets/sheet${i + 1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>`
    ).join('')
    + '</Types>';

  const rootRels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    + `<Relationships xmlns="${NS_PKG}">`
    + `<Relationship Id="rId1" Type="${NS_OFF}/officeDocument" Target="xl/workbook.xml"/>`
    + '</Relationships>';

  const workbook = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    + '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    + ` xmlns:r="${NS_OFF}"><sheets>`
    + named.map((s, i) =>
      `<sheet name="${xmlEscape(s.name)}" sheetId="${i + 1}" r:id="rId${i + 1}"/>`
    ).join('')
    + '</sheets></workbook>';

  const workbookRels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    + `<Relationships xmlns="${NS_PKG}">`
    + named.map((_, i) =>
      `<Relationship Id="rId${i + 1}" Type="${NS_OFF}/worksheet" Target="worksheets/sheet${i + 1}.xml"/>`
    ).join('')
    + '</Relationships>';

  return zip([
    { name: '[Content_Types].xml', data: contentTypes },
    { name: '_rels/.rels', data: rootRels },
    { name: 'xl/workbook.xml', data: workbook },
    { name: 'xl/_rels/workbook.xml.rels', data: workbookRels },
    ...named.map((s, i) => ({
      name: `xl/worksheets/sheet${i + 1}.xml`, data: sheetXml(s.headers),
    })),
  ]);
}

/** Hand the blob to the browser under a stable, dated filename. */
export function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 4000);
}

export function templateFilename() {
  const d = new Date();
  const stamp = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`
    + `-${String(d.getDate()).padStart(2, '0')}`;
  return `NetGravity_Upload_Template_${stamp}.xlsx`;
}
