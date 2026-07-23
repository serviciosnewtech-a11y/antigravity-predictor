#!/usr/bin/env bash
# =============================================================================
# tools/package_release.sh — build a clean release tarball for one product.
#
# Replaces the old approach of git-archiving everything and then rm -f'ing a
# manually maintained list of Docker files out of the bare-metal package (or
# vice versa). That list drifted from reality more than once — e.g.
# dashboard/nginx.conf was a Docker-only file that lived outside deploy/ and
# got missed, shipping a broken nginx config in a "clean" bare-metal build.
#
# Now the products are structurally separated on disk:
#   deploy/docker/      — everything Docker-only (compose file, Dockerfiles,
#                          deploy.sh, diagnose.sh, Makefile, etc.)
#   deploy/bare-metal/   — everything bare-metal-only (VPS installer, systemd
#                          units, nginx config)
# So packaging is now just: git-archive the ref, then delete the ONE
# directory that doesn't belong to the product being built. Nothing to keep
# in sync by hand.
#
# Usage:
#   bash tools/package_release.sh docker <git-ref> [output-dir]
#   bash tools/package_release.sh bare-metal <git-ref> [output-dir]
#
# Example:
#   bash tools/package_release.sh bare-metal beta-1.4 /tmp/releases
# =============================================================================
set -euo pipefail

die() { echo "[ERROR] $*" >&2; exit 1; }
log() { echo "[package] $*"; }

PRODUCT="${1:-}"
REF="${2:-}"
OUT_DIR="${3:-.}"

[[ "$PRODUCT" == "docker" || "$PRODUCT" == "bare-metal" ]] || \
    die "First arg must be 'docker' or 'bare-metal' (got: '${PRODUCT}')"
[[ -n "$REF" ]] || die "Second arg must be a git ref (tag/branch/commit) to package."

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

git rev-parse --verify "${REF}^{commit}" >/dev/null 2>&1 || die "Unknown git ref: ${REF}"

mkdir -p "$OUT_DIR"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

log "Exporting tracked files at ${REF} (git archive — untracked/ignored files never enter the package)…"
git archive "${REF}" | tar -x -C "$STAGE"

case "$PRODUCT" in
    bare-metal)
        log "Removing deploy/docker/ (Docker-only — not needed for bare metal)…"
        rm -rf "${STAGE}/deploy/docker"
        rm -f  "${STAGE}/.dockerignore"
        PKG_NAME="antigravity-predictor-bare-metal-${REF}"
        ;;
    docker)
        log "Removing deploy/bare-metal/ (bare-metal-only — not needed for Docker)…"
        rm -rf "${STAGE}/deploy/bare-metal"
        rm -f  "${STAGE}/run_monolith.sh" "${STAGE}/run_local.sh"
        PKG_NAME="antigravity-predictor-docker-${REF}"
        ;;
esac

TARBALL="${OUT_DIR}/${PKG_NAME}.tar.gz"
log "Writing ${TARBALL}…"
tar -czf "$TARBALL" -C "$(dirname "$STAGE")" "$(basename "$STAGE")" \
    --transform "s|^$(basename "$STAGE")|${PKG_NAME}|"

sha256sum "$TARBALL" > "${TARBALL}.sha256"
log "Done."
log "  $(cat "${TARBALL}.sha256")"
