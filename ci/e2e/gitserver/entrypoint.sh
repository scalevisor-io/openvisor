#!/bin/sh
# Seed the customer repository once, then serve it. The platform's merge sweep
# fetches refs/heads/main over SSH (§14 merge detection for non-forge remotes),
# so an EMPTY bare repo would park the run forever: main gets one commit here.
set -eu
REPO=/srv/git/${E2E_REPO_NAME:-todo}.git
if [ ! -d "$REPO" ]; then
  tmp=$(mktemp -d)
  git -C "$tmp" init -q -b main
  printf '# Todo\n\nCustomer repository seeded by the Openvisor e2e workflow.\n' > "$tmp/README.md"
  git -C "$tmp" -c user.name=e2e -c user.email=e2e@example.org add README.md
  git -C "$tmp" -c user.name=e2e -c user.email=e2e@example.org commit -q -m "Initial commit"
  git clone -q --bare "$tmp" "$REPO"
  git -C "$REPO" symbolic-ref HEAD refs/heads/main
  rm -rf "$tmp"
  chown -R git:git /srv/git
fi
touch /home/git/.ssh/authorized_keys
chown -R git:git /home/git/.ssh
chmod 600 /home/git/.ssh/authorized_keys
exec /usr/sbin/sshd -D -e
