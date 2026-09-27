#!/usr/bin/env bash
# Owner-only deploy. Every Cloudflare command goes through the guarded Ramen
# script, which refuses unless the Ramen Cloudflare account id and token are
# configured, so the machine's global wrangler login is never used.
#
#   scripts/deploy.sh setup    # once: D1 database, its migrations, the API key secret
#   scripts/deploy.sh worker   # the Python Worker (API + the UI as static assets)
#   scripts/deploy.sh pages    # the static site on Cloudflare Pages (the rule reader needs nothing else)
set -euo pipefail
GUARD="$HOME/RamenProtocol/_ops/infra/ramen-deploy.sh"
[ -x "$GUARD" ] || { echo "Guarded deploy script not found; refusing to run." >&2; exit 1; }
cd "$(dirname "$0")/.."

# Use the wrangler pinned in package.json (Python Workers need a recent one).
export PATH="$PWD/node_modules/.bin:$PATH"
PLACEHOLDER_DB="00000000-0000-0000-0000-000000000000"

case "${1:-}" in
  setup)
    if grep -q "$PLACEHOLDER_DB" wrangler.toml; then
      "$GUARD" run d1 create small-print-cache
      echo
      echo "Copy the database_id printed above into wrangler.toml ([[d1_databases]] database_id),"
      read -r -p "then press Enter to apply the migrations... " _
      if grep -q "$PLACEHOLDER_DB" wrangler.toml; then
        echo "wrangler.toml still has the placeholder database_id; stopping." >&2
        exit 1
      fi
    else
      echo "wrangler.toml already names a D1 database; skipping d1 create."
    fi
    "$GUARD" run d1 migrations apply small-print-cache --remote
    # Skip this prompt (Ctrl-C) to stay in demo mode: no key, no AI calls.
    "$GUARD" run secret put ANTHROPIC_API_KEY
    ;;
  worker)
    npm run build
    uv run pywrangler sync   # vendors the anthropic SDK into python_modules/
    exec "$GUARD"
    ;;
  pages)
    npm run build
    exec "$GUARD" pages dist --project-name small-print --branch main
    ;;
  *)
    echo "usage: scripts/deploy.sh setup|worker|pages" >&2
    exit 2
    ;;
esac
