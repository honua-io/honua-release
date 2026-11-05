import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

export const retiredPackage = "@honua/sdk-esri-compat";

export function dropRetiredDependency(manifest) {
  const dependencies = manifest?.dependencies;
  if (!dependencies || !Object.hasOwn(dependencies, retiredPackage)) {
    return false;
  }
  delete dependencies[retiredPackage];
  return true;
}

function writeInstalledManifest() {
  const manifestPath = new URL("./node_modules/@honua/honua-migrate/package.json", import.meta.url);
  if (!existsSync(manifestPath)) {
    return;
  }
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
  if (!dropRetiredDependency(manifest)) {
    return;
  }
  writeFileSync(manifestPath, JSON.stringify(manifest) + "\n");
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  writeInstalledManifest();
}
