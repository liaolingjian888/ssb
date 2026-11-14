#!/bin/bash
# 一键把 ssb-web 推到 GitHub（在本机执行，需要已登录 GitHub）
# 用法：bash deploy.sh
set -e
cd "$(dirname "$0")"

if ! git config user.email >/dev/null 2>&1; then
  git config user.email "liaolingjian888@users.noreply.github.com"
  git config user.name "liaolingjian888"
fi

git init -b main 2>/dev/null || true
git add -A
git commit -m "feat: 双色球自动选号与复盘（云端定时 + 网页）" || echo "无新改动"
git remote remove origin 2>/dev/null || true
git remote add origin https://github.com/liaolingjian888/ssb.git
git push -u origin main

echo ""
echo "已推送。接下来："
echo "1) 打开 https://github.com/liaolingjian888/ssb/actions 等待首次运行结束"
echo "2) 页面地址：https://liaolingjian888.github.io/ssb/"
