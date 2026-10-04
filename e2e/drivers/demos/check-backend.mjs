// Ask the reviewed site's own bootstrap whether this endpoint can be a demo backend.
import { readFileSync } from 'node:fs';
import { runInNewContext } from 'node:vm';
const window = { location: { search: '?apiBase=' + encodeURIComponent(process.env.E2E_BASE) } };
const document = {
  querySelector: () => ({ textContent: '<meta content="connect-src https://demo.honua.io; img-src https://demo.honua.io">' }),
  createElement: () => ({ setAttribute() {} }),
  head: { appendChild() {} },
};
runInNewContext(readFileSync(process.argv[2], 'utf8'), { window, document }, { timeout: 1000 });
process.exit(window.HONUA_DEMO_BACKEND_ORIGIN ? 0 : 1);
