#!/usr/bin/env bash
# Owner-only deploy. The only deploy path is the guarded Ramen script, which
# refuses unless the Ramen Cloudflare account id and token are configured, so
# the machine's global wrangler login is never used.
#
#   scripts/deploy.sh worker   # the Python Worker (API + the UI as static assets)
#   scripts/deploy.sh pages    # the static UI on Cloudflare Pages
#
# One-time setup (D1 database, migrations, the ANTHROPIC_API_KEY secret) is
# described in the README and must also run under the Ramen account.
set -euo pipefail
GUARD="$HOME/RamenProtocol/_ops/infra/ramen-deploy.sh"
[ -x "$GUARD" ] || { echo "Guarded deploy script not found; refusing to deploy." >&2; exit 1; }
cd "$(dirname "$0")/.."

npm run build
# Use the wrangler pinned in package.json (Python Workers need a recent one).
export PATH="$PWD/node_modules/.bin:$PATH"

case "${1:-}" in
  worker)
    uv run pywrangler sync   # vendors the anthropic SDK into python_modules/
    exec "$GUARD"
    ;;
  pages)
    exec "$GUARD" pages dist --project-name small-print
    ;;
  *)
    echo "usage: scripts/deploy.sh worker|pages" >&2
    exit 2
    ;;
esac
