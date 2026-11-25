const ROWS = 500;
const COLUMNS = 60;
const MAX_INFLATED_BYTES = 64 * 1024 ** 2;
let workbook;
let sheets;

function inspectArchive(bytes) {
  const view = new DataView(bytes);
  const finalOffset = bytes.byteLength - 22;
  for (let end = finalOffset; end >= Math.max(0, finalOffset - 65535); end--) {
    if (view.getUint32(end, true) !== 0x06054b50 || end + 22 + view.getUint16(end + 20, true) !== bytes.byteLength) continue;
    let offset = view.getUint32(end + 16, true);
    const count = view.getUint16(end + 10, true);
    if (count > 2500) break;
    let size = 0;
    for (let entry = 0; entry < count; entry++) {
      if (offset + 46 > end || view.getUint32(offset, true) !== 0x02014b50) throw new Error('无法读取文档压缩目录。');
      size += view.getUint32(offset + 24, true);
      if (size > MAX_INFLATED_BYTES) throw new Error('文档解压后过大，请下载查看。');
      offset += 46 + view.getUint16(offset + 28, true) + view.getUint16(offset + 30, true) + view.getUint16(offset + 32, true);
    }
    return;
  }
  throw new Error('无法读取此文档，请下载查看。');
}

function csvText(bytes) {
  try { return new TextDecoder('utf-8', { fatal: true }).decode(bytes); }
  catch { return new TextDecoder('gb18030').decode(bytes); }
}

function table(index) {
  const worksheet = workbook.Sheets[workbook.SheetNames[index]];
  if (!worksheet?.['!ref']) return { rows: [], cropped: false };
  const range = sheets.utils.decode_range(worksheet['!ref']);
  const original = sheets.utils.decode_range(worksheet['!fullref'] ?? worksheet['!ref']);
  const cropped = original.e.r - range.s.r >= ROWS || original.e.c - range.s.c >= COLUMNS;
  range.e.r = Math.min(range.e.r, range.s.r + ROWS - 1);
  range.e.c = Math.min(range.e.c, range.s.c + COLUMNS - 1);
  const rows = sheets.utils.sheet_to_json(worksheet, { range, header: 1, raw: false, defval: '' });
  return { rows, cropped };
}

self.onmessage = async ({ data }) => {
  try {
    if ('index' in data) {
      self.postMessage({ ...table(data.index), index: data.index });
      return;
    }
    const { type, buffer } = data;
    if (type !== 'csv') inspectArchive(buffer);
    if (type === 'docx') {
      const { default: mammoth } = await import('mammoth/mammoth.browser.js');
      const imageTypes = new Set(['image/png', 'image/jpeg', 'image/gif', 'image/webp']);
      const options = { includeEmbeddedStyleMap: false, externalFileAccess: false,
        convertImage: mammoth.images.imgElement(async (image) => imageTypes.has(image.contentType)
          ? { src: `data:${image.contentType};base64,${await image.read('base64')}` } : {}) };
      const html = (await mammoth.convertToHtml({ arrayBuffer: buffer }, options)).value;
      self.postMessage({ html });
      return;
    }
    sheets = await import('xlsx');
    workbook = sheets.read(type === 'csv' ? csvText(buffer) : buffer, {
      type: type === 'csv' ? 'string' : 'array', sheetRows: ROWS + 1, cellHTML: false, raw: type === 'csv',
    });
    if (!workbook.SheetNames.length) throw new Error('此文件没有可预览的工作表。');
    self.postMessage({ names: workbook.SheetNames, index: 0, ...table(0) });
  } catch (error) {
    self.postMessage({ error: error instanceof Error ? error.message.slice(0, 180) : '无法预览此文件。' });
  }
};
