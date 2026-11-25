export const PREVIEW_LIMIT = 15_000_000;
export const documentType = (filename) => /\.(pdf|docx|xlsx|csv)$/i.exec(filename ?? '')?.[1].toLowerCase();
