#!/usr/bin/env bash
# 記録ファイルをコミットして push する(Actions のループ用)。
#   使い方: bash tools/push_logs.sh "コミットメッセージ" パス...
# pull --rebase がぶつかった時は rebase を中止して元に戻し、次の回にやり直す
# (2026-10-18、重なって動いた回で rebase が途中のまま止まり、以降の記録が push できなかった反省)。
msg="$1"; shift
git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"
# 存在するものだけ add する(1つでも無いパスが混ざると git add は全部やめてしまうため)
for p in "$@"; do
  [ -e "$p" ] && git add "$p"
done
if ! git diff --cached --quiet; then
  git commit -q -m "$msg"
fi
# 未 push のコミットが無ければ終わり
if [ -z "$(git log origin/main..HEAD --oneline 2>/dev/null)" ]; then
  git fetch -q origin main 2>/dev/null || true
  [ -z "$(git log origin/main..HEAD --oneline 2>/dev/null)" ] && exit 0
fi
for t in 1 2 3; do
  if git pull -q --rebase origin main; then
    git push -q origin main && exit 0
  else
    git rebase --abort 2>/dev/null || true
  fi
  sleep 10
done
echo "push できず(次の回にやり直す)"
exit 0
