export function scratchpadTitle(item, textTitle) {
  return item.kind === 'text' ? textTitle : item.filename;
}

function searchable(value) {
  return (value ?? '').normalize('NFKC').toLowerCase();
}

export function filterScratchpadItems(items, query, textTitle) {
  const term = searchable(query.trim());
  if (!term) return items;
  return items.filter((item) => [scratchpadTitle(item, textTitle), item.text]
    .some((value) => searchable(value).includes(term)));
}
