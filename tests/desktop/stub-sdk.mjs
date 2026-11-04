// Installs a minimal node_modules stub of the Desktop SDK so plugin.js imports
// resolve under plain `node --test` (the real SDK exists only inside the app).
import fs from 'node:fs/promises'
import path from 'node:path'
const nm = path.resolve(path.dirname(new URL(import.meta.url).pathname), '../../node_modules')
async function mod(name, code) {
  const dir = path.join(nm, name)
  await fs.mkdir(dir, { recursive: true })
  await fs.writeFile(path.join(dir, 'package.json'), JSON.stringify({ name, type: 'module', main: 'index.mjs' }))
  await fs.writeFile(path.join(dir, 'index.mjs'), code)
}
await mod('react', 'export const useState = (v) => [v, () => {}]; export const useEffect = () => {}; export default {}')
await fs.writeFile(path.join(nm, 'react', 'jsx-runtime.mjs'), 'export const jsx = (type, props) => ({ type, props }); export const jsxs = jsx')
await fs.writeFile(path.join(nm, 'react', 'package.json'), JSON.stringify({ name: 'react', type: 'module', exports: { '.': './index.mjs', './jsx-runtime': './jsx-runtime.mjs' } }))
await mod('@hermes/plugin-sdk', "const C = ({ children }) => children ?? null; export const Codicon = C, Tip = C; export const host = { onEvent: () => () => {}, state: {} }; export const useValue = (a) => (a && typeof a.get === 'function' ? a.get() : ''); export const STATUSBAR_AREAS = { left: 'statusBar.left', right: 'statusBar.right' }")
