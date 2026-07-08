@echo off
chcp 932 > nul
cd /d "%~dp0"

echo.
echo [GitHub] クラウドの最新データを取り込んでから、ローカルの変更を反映します...
echo.

git pull --rebase origin main
git add -A
git diff --cached --quiet
if %errorlevel%==0 (
    echo ローカルに新しい変更はありません。取り込みのみ完了。
) else (
    git commit -m "local update"
    git push origin main
)

echo.
echo 上にエラーが出ていなければ完了です。この窓は閉じてOK。
echo.
pause