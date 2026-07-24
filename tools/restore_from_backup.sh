#!/usr/bin/env bash
# =============================================================================
# tools/restore_from_backup.sh -- restore /opt/predictor from
# /opt/predictor-backups/ (or an alternate source/target).
#
# Companion to tools/backup_signal_log.py, tools/backup_forge_db.py,
# tools/backup_config_and_secrets.py. See docs/RESTORE_PLAYBOOK.md for the
# full runbook and worked scenarios; this script is the sharp end of that
# playbook.
#
# Snapshot families this tool picks from (see DATA_INVENTORY rows 1/2/4-10):
#   signal_history.<stamp>.db      -> $TARGET/logs/signal_history.db
#   forge.<stamp>.db               -> $TARGET/forge_data/forge.db
#   configstate.<stamp>.tar.gz     -> extracted over $TARGET (+ /etc/nginx/.htpasswd)
#
# Timestamps are `YYYYMMDD-HHMMSS[-NNNNNN]` (UTC). Default: pick the newest
# per family. With `--timestamp T`, pick the newest snapshot at-or-before T
# in each family (point-in-time recovery).
#
# Idempotent, refuses to run against an active predictor.service without
# --force, dry-run flag prints what it would do without touching anything,
# and writes an audit receipt to $TARGET/logs/restore_applied.log on every
# real run.
#
# Usage:
#   bash tools/restore_from_backup.sh \
#       --source-dir /opt/predictor-backups \
#       --target-dir /opt/predictor \
#       [--timestamp YYYYMMDD-HHMMSS] \
#       [--only signal_history|forge|configstate] \
#       [--dry-run] [--force] [--help]
# =============================================================================
set -euo pipefail

SOURCE_DIR=""
TARGET_DIR=""
TIMESTAMP=""
ONLY=""
DRY_RUN=0
FORCE=0

die() { echo "[ERROR] $*" >&2; exit 1; }
log() { echo "[restore] $*"; }
plan() { echo "[restore:plan] $*"; }
apply() { echo "[restore:apply] $*"; }

usage() {
    sed -n '2,30p' "$0"
    exit 0
}

while (( $# > 0 )); do
    case "$1" in
        --source-dir) SOURCE_DIR="$2"; shift 2 ;;
        --target-dir) TARGET_DIR="$2"; shift 2 ;;
        --timestamp)  TIMESTAMP="$2"; shift 2 ;;
        --only)       ONLY="$2"; shift 2 ;;
        --dry-run)    DRY_RUN=1; shift ;;
        --force)      FORCE=1; shift ;;
        --help|-h)    usage ;;
        *) die "Unknown arg: $1 (see --help)" ;;
    esac
done

[[ -n "$SOURCE_DIR" ]] || die "--source-dir is required"
[[ -n "$TARGET_DIR" ]] || die "--target-dir is required"
[[ -d "$SOURCE_DIR" ]] || die "--source-dir $SOURCE_DIR is not a directory"
case "${ONLY:-all}" in
    ""|"all"|"signal_history"|"forge"|"configstate") ;;
    *) die "--only must be one of: signal_history, forge, configstate (or omit for all)" ;;
esac

# Safety: refuse to run against an active predictor.service unless --force.
# The typical fatal footgun is running restore while predictor is holding
# signal_history.db open -- the copy succeeds but the running process
# never sees it. Force lets tests/drills bypass this against scratch dirs.
if (( ! FORCE )) && command -v systemctl &>/dev/null; then
    if systemctl is-active --quiet predictor.service 2>/dev/null; then
        die "predictor.service is active on this host. Stop it first (systemctl stop predictor.service) or pass --force if you're restoring into a scratch dir."
    fi
fi

# Pick the snapshot to restore for a given family.
# Args: $1 = family glob prefix (e.g. "signal_history.", "forge.", "configstate.")
#       $2 = family filename suffix (e.g. ".db", ".tar.gz")
# Emits the chosen filename on stdout, or empty if no candidate.
pick_snapshot() {
    local prefix="$1"
    local suffix="$2"
    # Find candidates sorted ascending by filename (which sorts by
    # timestamp because YYYYMMDD-HHMMSS is lexicographic-safe).
    local -a candidates
    mapfile -t candidates < <(ls -1 "$SOURCE_DIR" 2>/dev/null | grep -E "^${prefix}[0-9]{8}-[0-9]{6}(-[0-9]+)?${suffix//./\\.}\$" | sort)
    if (( ${#candidates[@]} == 0 )); then
        echo ""
        return
    fi

    if [[ -z "$TIMESTAMP" ]]; then
        # Default: newest.
        echo "${candidates[-1]}"
        return
    fi

    # Point-in-time: pick the newest whose embedded stamp is <= $TIMESTAMP.
    # Extract the stamp portion (strip prefix + suffix); compare
    # lexicographically. $TIMESTAMP may or may not include the microsecond
    # tail; pad it deterministically for comparison.
    local pick=""
    local cand_stamp
    for c in "${candidates[@]}"; do
        cand_stamp="${c#$prefix}"
        cand_stamp="${cand_stamp%$suffix}"
        # Compare only up to the length of $TIMESTAMP -- lets "20260724-150000"
        # match snapshots whose full stamp is "20260724-150000-123456".
        if [[ "${cand_stamp:0:${#TIMESTAMP}}" > "$TIMESTAMP" ]]; then
            break
        fi
        pick="$c"
    done
    echo "$pick"
}

want_family() {
    [[ -z "$ONLY" || "$ONLY" == "all" || "$ONLY" == "$1" ]]
}

log "Source: $SOURCE_DIR"
log "Target: $TARGET_DIR"
[[ -n "$TIMESTAMP" ]] && log "Timestamp cutoff: $TIMESTAMP (pick newest at-or-before)" || log "Timestamp: newest per family"
[[ -n "$ONLY" && "$ONLY" != "all" ]] && log "Restricted to family: $ONLY"
(( DRY_RUN )) && log "DRY RUN -- no filesystem changes will be made."

# Discover picks up-front so the plan is fully known before any writes.
PICK_SIGNAL=""
PICK_FORGE=""
PICK_CONFIG=""
if want_family signal_history; then
    PICK_SIGNAL="$(pick_snapshot 'signal_history.' '.db')"
fi
if want_family forge; then
    PICK_FORGE="$(pick_snapshot 'forge.' '.db')"
fi
if want_family configstate; then
    PICK_CONFIG="$(pick_snapshot 'configstate.' '.tar.gz')"
fi

# Print plan.
if want_family signal_history; then
    if [[ -n "$PICK_SIGNAL" ]]; then
        plan "signal_history: $PICK_SIGNAL -> $TARGET_DIR/logs/signal_history.db"
    else
        plan "signal_history: no snapshot found (skip)"
    fi
fi
if want_family forge; then
    if [[ -n "$PICK_FORGE" ]]; then
        plan "forge:           $PICK_FORGE -> $TARGET_DIR/forge_data/forge.db"
    else
        plan "forge:           no snapshot found (skip)"
    fi
fi
if want_family configstate; then
    if [[ -n "$PICK_CONFIG" ]]; then
        plan "configstate:    $PICK_CONFIG extracted over $TARGET_DIR (+ /etc/nginx/.htpasswd if present)"
    else
        plan "configstate:    no snapshot found (skip)"
    fi
fi

# If nothing to do at all, exit cleanly.
if [[ -z "$PICK_SIGNAL" && -z "$PICK_FORGE" && -z "$PICK_CONFIG" ]]; then
    log "Nothing to restore. Exiting."
    exit 0
fi

if (( DRY_RUN )); then
    log "Dry run complete."
    exit 0
fi

# ── APPLY ──────────────────────────────────────────────────────────────────

mkdir -p "$TARGET_DIR/logs" "$TARGET_DIR/forge_data" "$TARGET_DIR/models" "$TARGET_DIR/src"

# Receipt buffer -- flushed at the end.
RECEIPT_LINES=()
RECEIPT_LINES+=("=== restore_applied at $(date -u +%Y-%m-%dT%H:%M:%SZ) ===")
RECEIPT_LINES+=("source_dir: $SOURCE_DIR")
RECEIPT_LINES+=("target_dir: $TARGET_DIR")
[[ -n "$TIMESTAMP" ]] && RECEIPT_LINES+=("timestamp_cutoff: $TIMESTAMP")
[[ -n "$ONLY" && "$ONLY" != "all" ]] && RECEIPT_LINES+=("restricted_to: $ONLY")

sha256_of() {
    if command -v sha256sum &>/dev/null; then
        sha256sum "$1" | awk '{print $1}'
    else
        openssl dgst -sha256 "$1" | awk '{print $NF}'
    fi
}

# 1. signal_history.
if [[ -n "$PICK_SIGNAL" ]]; then
    src="$SOURCE_DIR/$PICK_SIGNAL"
    dst="$TARGET_DIR/logs/signal_history.db"
    apply "cp $src -> $dst"
    cp "$src" "$dst"
    chmod 600 "$dst"
    if id predictor &>/dev/null; then
        chown predictor:predictor "$dst" 2>/dev/null || true
    fi
    RECEIPT_LINES+=("signal_history: $PICK_SIGNAL sha256=$(sha256_of "$src")")
fi

# 2. forge.
if [[ -n "$PICK_FORGE" ]]; then
    src="$SOURCE_DIR/$PICK_FORGE"
    dst="$TARGET_DIR/forge_data/forge.db"
    apply "cp $src -> $dst"
    cp "$src" "$dst"
    chmod 600 "$dst"
    if id predictor &>/dev/null; then
        chown predictor:predictor "$dst" 2>/dev/null || true
    fi
    RECEIPT_LINES+=("forge: $PICK_FORGE sha256=$(sha256_of "$src")")
fi

# 3. configstate.
if [[ -n "$PICK_CONFIG" ]]; then
    src="$SOURCE_DIR/$PICK_CONFIG"
    apply "extract $src into $TARGET_DIR (and /etc/nginx/.htpasswd if present in tarball)"
    RECEIPT_LINES+=("configstate: $PICK_CONFIG sha256=$(sha256_of "$src")")

    # Walk tar contents; place each entry at its target path.
    # Deliberately explicit rather than a single `tar -xf ...`: the tarball
    # contains one path that lands OUTSIDE $TARGET_DIR (etc/nginx/.htpasswd
    # -> /etc/nginx/.htpasswd), which a naive extraction would put
    # UNDER $TARGET_DIR/etc/nginx/.htpasswd -- wrong. This loop routes each
    # arcname to the right destination.
    EXTRACT_TMP=$(mktemp -d)
    trap 'rm -rf "$EXTRACT_TMP"' EXIT
    tar -xzf "$src" -C "$EXTRACT_TMP"
    while IFS= read -r arcname; do
        srcfile="$EXTRACT_TMP/$arcname"
        [[ -f "$srcfile" ]] || continue
        case "$arcname" in
            etc/nginx/.htpasswd)
                if [[ -w /etc/nginx ]] || [[ $EUID -eq 0 ]]; then
                    apply "  place $arcname -> /etc/nginx/.htpasswd"
                    cp "$srcfile" /etc/nginx/.htpasswd
                    chmod 640 /etc/nginx/.htpasswd 2>/dev/null || true
                    # Only chown if www-data exists (standard on Ubuntu/Debian)
                    if id www-data &>/dev/null; then
                        chown root:www-data /etc/nginx/.htpasswd 2>/dev/null || true
                    fi
                    RECEIPT_LINES+=("  placed /etc/nginx/.htpasswd from tarball")
                else
                    apply "  skip $arcname -> /etc/nginx/.htpasswd (not writable; run as root to restore htpasswd)"
                    RECEIPT_LINES+=("  SKIPPED /etc/nginx/.htpasswd (target not writable)")
                fi
                ;;
            .env)
                dst="$TARGET_DIR/.env"
                mkdir -p "$(dirname "$dst")"
                apply "  place $arcname -> $dst"
                cp "$srcfile" "$dst"
                chmod 600 "$dst"
                if id predictor &>/dev/null; then
                    chown predictor:predictor "$dst" 2>/dev/null || true
                fi
                ;;
            *)
                dst="$TARGET_DIR/$arcname"
                mkdir -p "$(dirname "$dst")"
                apply "  place $arcname -> $dst"
                cp "$srcfile" "$dst"
                if id predictor &>/dev/null; then
                    chown predictor:predictor "$dst" 2>/dev/null || true
                fi
                ;;
        esac
    done < <(tar -tzf "$src")
    rm -rf "$EXTRACT_TMP"
    trap - EXIT
fi

# Write the receipt.
RECEIPT_PATH="$TARGET_DIR/logs/restore_applied.log"
mkdir -p "$(dirname "$RECEIPT_PATH")"
{
    for line in "${RECEIPT_LINES[@]}"; do
        printf '%s\n' "$line"
    done
    printf '=== end ===\n\n'
} >> "$RECEIPT_PATH"
if id predictor &>/dev/null; then
    chown predictor:predictor "$RECEIPT_PATH" 2>/dev/null || true
fi

log "Restore complete. Receipt appended to $RECEIPT_PATH."
log "Restart services: systemctl start predictor.service signal_agent.service predictor_backup.timer forge_backup.timer config_backup.timer"
