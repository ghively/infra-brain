// Post-build EOL normalization (audit F15 follow-up, 2026-07-12).
// Vite emits CRLF in index.html when building on Windows, while Linux CI
// emits LF — that byte difference trips the dashboard-app-lint staleness
// gate (`git diff --exit-code -- static2/`). Rewriting every text artifact
// to LF makes the committed build identical regardless of build host.
// static2/** is also marked `-text` in .gitattributes so git never
// re-converts the bytes on checkout.
import { readdirSync, readFileSync, writeFileSync, statSync } from "node:fs";
import { join } from "node:path";

const OUT_DIR = new URL("../../src/infra_brain/dashboard/static2", import.meta.url).pathname
  // On Windows the URL pathname carries a leading slash before the drive letter.
  .replace(/^\/([A-Za-z]:)/, "$1");
const TEXT_EXT = /\.(html|js|css|svg|json|txt|map)$/i;

function walk(dir) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) {
      walk(p);
    } else if (TEXT_EXT.test(name)) {
      const before = readFileSync(p);
      // Strip every CR, not just CRLF pairs — Vite's Windows HTML output can
      // leave a lone \r (observed on the #root line, CI pipeline 1191).
      const after = Buffer.from(before.toString("utf8").replace(/\r/g, ""), "utf8");
      if (!after.equals(before)) {
        writeFileSync(p, after);
        console.log(`normalize-eol: rewrote ${p} to LF`);
      }
    }
  }
}

walk(OUT_DIR);
