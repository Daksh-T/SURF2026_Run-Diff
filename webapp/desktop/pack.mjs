// Package Run·Diff into a standalone .app using @electron/packager.
//
// electron-builder does not run under this machine's bun-as-node shim (source-map-support /
// bluebird crash before reaching build logic). @electron/packager is lighter and works. It emits
// an unsigned .app bundle in release/. We carry the PyInstaller sidecar and the built frontend
// dist as extraResource entries, landing them in Contents/Resources/ where main.js looks
// (process.resourcesPath / {rundiff-backend, frontend-dist}).

import { packager } from "@electron/packager";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const webapp = path.resolve(__dirname, "..");

const opts = {
  dir: __dirname,
  name: "Run·Diff",
  out: path.join(__dirname, "release"),
  overwrite: true,
  platform: "darwin",
  arch: "arm64",
  appBundleId: "edu.sewanee.surf.rundiff",
  appCategoryType: "public.app-category.education",
  icon: path.join(webapp, "branding", "AppIcon.icns"), // database + grad-cap mark
  // unsigned — no Developer ID identity configured on this machine
  osxSign: false,
  // ignore everything we don't need in the asar (keep it lean; sidecar+frontend ride as resources)
  ignore: [
    /^\/release/,
    /^\/pack\.mjs$/,
    /^\/\.gitignore$/,
  ],
  extraResource: [
    path.join(webapp, "backend", "dist_backend", "rundiff-backend"),
    path.join(webapp, "frontend", "dist"),
  ],
};

const appPaths = await packager(opts);
console.log("packaged:", appPaths);
