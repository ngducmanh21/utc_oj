// Copy the pinned browser distribution into Django's collected static assets.
import {copyFile, mkdir, readFile, writeFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';

const root = new URL('../', import.meta.url);
const target = new URL('resources/vendor/tus/', root);
await mkdir(target, {recursive: true});
for (const [source, name] of [
  ['dist/tus.min.js', 'tus.min.js'],
  ['dist/tus.min.js.map', 'tus.min.js.map'],
]) {
  await copyFile(fileURLToPath(new URL(`node_modules/tus-js-client/${source}`, root)),
    fileURLToPath(new URL(name, target)));
}
const license = await readFile(new URL('node_modules/tus-js-client/LICENSE', root), 'utf8');
await writeFile(new URL('LICENSE', target), license.trimEnd() + '\n');
