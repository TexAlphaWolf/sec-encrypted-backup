#!/usr/bin/env bash
# End to end inside the image: init, backup, verify, rotate, restore, with a read-only root
# filesystem and no capabilities. Needs Docker. Usage: tools/docker_e2e.sh [image]
set -euo pipefail
IMAGE="${1:-encbackup}"
VOL="encbackup-e2e-$$"
WORK="$(mktemp -d)"
trap 'docker volume rm -f "$VOL" >/dev/null 2>&1 || true; rm -rf "$WORK"' EXIT

mkdir -p "$WORK/data/sub"
echo "hello" > "$WORK/data/hello.txt"
head -c 200000 /dev/urandom > "$WORK/data/sub/random.bin"
chmod -R a+rX "$WORK/data"

RUN=(docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp
     -v "$VOL":/state -v "$WORK/data":/data:ro "$IMAGE")

# the volume is created from the image, owned by the unprivileged user
[ "$(docker run --rm --entrypoint id "$IMAGE" -u)" != 0 ]

# the recovery key is made elsewhere in real life; here, in a throw-away container
docker run --rm --entrypoint age-keygen "$IMAGE" > "$WORK/recovery.txt" 2>/dev/null
RECOVERY_PUB="$(docker run --rm -i --entrypoint age-keygen "$IMAGE" -y < "$WORK/recovery.txt")"

"${RUN[@]}" init --recovery-recipient "$RECOVERY_PUB"
"${RUN[@]}" backup --verify | tee "$WORK/backup.txt"
grep -q "result: OK" "$WORK/backup.txt"
"${RUN[@]}" verify --json | tee "$WORK/verify.json"
grep -q '"ok": true' "$WORK/verify.json"
"${RUN[@]}" rotate --new-identity
"${RUN[@]}" verify
"${RUN[@]}" status | tee "$WORK/status.txt"
grep -q "result: OK" "$WORK/status.txt"

# restore, then compare with the original inside a container that sees both
"${RUN[@]}" restore --target /state/restored
docker run --rm -v "$VOL":/state:ro -v "$WORK/data":/data:ro --entrypoint sh "$IMAGE" -c \
  'diff -r /data /state/restored/data && echo "restored tree is identical"'

# the offline recovery identity opens the same password (it is put in the volume only for this check)
docker run --rm -i -v "$VOL":/state --entrypoint sh "$IMAGE" -c \
  'umask 077 && cat > /state/recovery.txt' < "$WORK/recovery.txt"
"${RUN[@]}" verify --recovery-identity /state/recovery.txt | tee "$WORK/recovery-verify.txt"
grep -q "recovery key: opens the repository password" "$WORK/recovery-verify.txt"
echo "docker end-to-end: OK"
