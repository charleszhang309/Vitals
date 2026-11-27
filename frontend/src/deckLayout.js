export const grids = { '3x2': [3, 2], '4x3': [4, 3], '5x3': [5, 3], '6x3': [6, 3] };

export function fitsDeckGrid(scene, grid = scene.grid) {
  const [columns, rows] = grids[grid];
  return scene.tiles.every((tile, index) => !tile || (index < columns * rows && (
    tile.span !== 2 || ((index + 1) % columns !== 0 && !scene.tiles[index + 1])
  )));
}

export function deckSlots(scene) {
  const [columns, rows] = grids[scene.grid];
  return Array.from({ length: columns * rows }, (_, index) => index)
    .filter((index) => index % columns === 0 || scene.tiles[index - 1]?.span !== 2);
}
