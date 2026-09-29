/**
 * Same-origin path the committed fixture service answers on.
 *
 * `vite.config.ts` serves the fixture here in both `npm run dev` and
 * `npm run preview`, and `src/main.ts` connects here whenever no live endpoint
 * is configured. Keeping the path in one module is what makes the default lane
 * work with no account, no key, and no third-party network call.
 */
export const FIXTURE_LAYER_PATH = "/fixture/rest/services/honua-demo/FeatureServer/0";
