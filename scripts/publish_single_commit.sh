#!/usr/bin/env bash
# Publish the staged files as the only commit on BRANCH, dated at the Unix epoch
# (1970-01-01T00:00:00Z, the earliest date git and GitHub accept). This repository keeps
# no history: every refresh replaces that single commit with a force push.
set -euo pipefail

branch="${1:?usage: publish_single_commit.sh BRANCH}"
head="$(git rev-parse HEAD)"
tree="$(git write-tree)"
if [[ "$tree" == "$(git rev-parse 'HEAD^{tree}')" && "$(git rev-list --count HEAD)" == 1 \
      && "$(git log -1 --format='%at %ct' HEAD)" == "0 0" ]]; then
  printf 'Already a single epoch commit with these files; nothing to publish.\n'
  exit 0
fi

# Keep whoever authored the first commit, so CI never adds its own identity to the history.
root="$(git rev-list --max-parents=0 HEAD | tail -n 1)"
GIT_AUTHOR_NAME="$(git log -1 --format=%an "$root")"
GIT_AUTHOR_EMAIL="$(git log -1 --format=%ae "$root")"
export GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL
export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME" GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"
export GIT_AUTHOR_DATE='@0 +0000' GIT_COMMITTER_DATE='@0 +0000'
commit="$(git commit-tree "$tree" -m 'Initial commit')"

# The lease fails the push, instead of dropping work, if the branch moved since checkout.
git push --force-with-lease="refs/heads/$branch:$head" origin "$commit:refs/heads/$branch"
printf 'Published %s as the only commit on %s.\n' "${commit:0:7}" "$branch"
