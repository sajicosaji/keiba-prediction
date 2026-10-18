#!/usr/bin/env bash
# 記録ファイルをコミットして push する(Actions のループ用)。
#   使い方: bash tools/push_logs.sh "コミットメッセージ" パス...
# ぶつかった時(別の係が同じファイルを先に push した時):
#   - 記録の表(CSV)は .gitattributes の merge=union で両方の行を残す(追記だけなので失われない)
#   - それ以外(サイト用JSONなど毎回作り直すもの)は手元の新しい方を採用(-X theirs = rebase で載せ直す側)
#   - それでもだめなら rebase を中止して元に戻し、次の回にやり直す
# (2026-10-18 試験で、ぶつかると以後その日ずっと push できなくなることを確かめて直した)
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
git fetch -q origin main 2>/dev/null || true
[ -z "$(git log origin/main..HEAD --oneline 2>/dev/null)" ] && exit 0
for t in 1 2 3; do
  if git pull -q --rebase origin main 2>/dev/null; then
    git push -q origin main && exit 0
  else
    git rebase --abort 2>/dev/null || true
    if git pull -q --rebase -X theirs origin main 2>/dev/null; then
      git push -q origin main && exit 0
    else
      git rebase --abort 2>/dev/null || true
    fi
  fi
  sleep 10
done
echo "push できず(次の回にやり直す)"
exit 0
