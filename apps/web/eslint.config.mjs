import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
    // Vendored, unmodified third-party file (frozen snapshot of
    // @soundtouchjs/audio-worklet's .dist/soundtouch-processor.js -- see the comment at
    // SOUNDTOUCH_PROCESSOR_URL in lib/player.ts). Not authored or maintained by this project;
    // its lint warnings aren't actionable here.
    "public/soundtouch-processor.js",
  ]),
]);

export default eslintConfig;
