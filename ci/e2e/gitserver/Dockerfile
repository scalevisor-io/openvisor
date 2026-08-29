# CI e2e: the customer's git hosting, in a box. sshd + git serving one bare
# repository over the compose `internal` network, so the §14 pipeline's push
# preflight (hidden-ref probe), branch push (runner sandbox) and merge detection
# (worker, over SSH) all run against a real remote with no external forge and
# no secret. CI-only: never part of make dev / make prod.
FROM alpine:3.20
RUN apk add --no-cache openssh-server git \
 && adduser -D -s /usr/bin/git-shell git \
 # adduser leaves the account locked ("!"), which sshd refuses even for public
 # keys; "*" means "no password" and keeps key auth working.
 && sed -i 's/^git:!/git:*/' /etc/shadow \
 && echo /usr/bin/git-shell >> /etc/shells \
 && mkdir -p /home/git/.ssh /srv/git \
 && chown -R git:git /home/git /srv/git \
 && chmod 700 /home/git/.ssh \
 # the hooks run as `git`; this keeps root-run `docker compose exec` debugging
 # commands from tripping git's ownership check on the git-owned repository
 && git config --system --add safe.directory '*' \
 && ssh-keygen -A
COPY sshd_config /etc/ssh/sshd_config
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 22
ENTRYPOINT ["/entrypoint.sh"]
