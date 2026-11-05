#!/bin/sh
# 口袋代理 PocketProxy 安装/升级(在 iPhone 的 iSH 里运行,重复运行就是升级)。
# 用法: apk add curl && curl -fsSL --resolve icatdog.dynv6.net:443:47.76.241.196 https://icatdog.dynv6.net/rpa-update/pocketproxy/install.sh | sh
# 备用(香港服务器打不开时): apk add curl && curl -fsSL https://raw.githubusercontent.com/ssdawjss/d-i13whabio2/main/install.sh | sh
# 装好后每次打开 iSH 自动在后台启动代理(默认端口 8080),iSH 里直接显示电脑要填的地址和端口。
BASE=https://icatdog.dynv6.net/rpa-update/pocketproxy
GH=https://raw.githubusercontent.com/ssdawjss/d-i13whabio2/main
LOGF=/root/pocketproxy.log
# 发布签名公钥(和 pocketproxy.py 里的 SIGN_PUBKEY 相同,由 keygen.sh 写入)。程序必须用对应私钥签过名才会装
PUBKEY='MCowBQYDK2VwAyEA9O1YjAhlzFrGr0Mfn++Zs2nlNr6GsruY2qZ19oijMhE='
# 从第 $1 个下载地址下载 $2 存到 $3:1=香港固定地址(国内解析域名常失败,证书照常校验) 2=香港按域名 3=GitHub
get() {
  case $1 in
    1) curl -fsSL -m 60 --resolve icatdog.dynv6.net:443:47.76.241.196 -o "$3" "$BASE/$2" ;;
    2) curl -fsSL -m 60 -o "$3" "$BASE/$2" ;;
    3) curl -fsSL -m 60 -o "$3" "$GH/$2" ;;
  esac 2>/dev/null
}
# 校验签名:通过返回 0
verify() {
  [ -n "$PUBKEY" ] || return 1
  { echo '-----BEGIN PUBLIC KEY-----'; echo "$PUBKEY"; echo '-----END PUBLIC KEY-----'; } > /root/.pocketproxy.pub
  openssl pkeyutl -verify -pubin -inkey /root/.pocketproxy.pub -rawin -in "$1" -sigfile "$2" >/dev/null 2>&1
  r=$?; rm -f /root/.pocketproxy.pub; return $r
}
# 写一条记录到日志(只有 UTC 秒数,程序显示时换成本地时间),用来事后看 iSH 有没有被重新打开过
mark() { echo "@$(date +%s) $*" >> "$LOGF"; }
echo "== 口袋代理 PocketProxy 安装/升级 =="
mark "运行了安装/升级命令"

if command -v python3 >/dev/null 2>&1; then
  echo "[1/3] python3 已经装好,跳过"
else
  echo "[1/3] 安装 python3(约 14MB,iSH 解压很慢,常要 5-15 分钟;下面有进度条,请等着别切走)"
  head -n 1 /etc/apk/repositories
  apk add --progress python3 || { echo "python3 安装失败,请检查网络后重新运行这条命令"; exit 1; }
fi
# openssl 用来校验程序签名(约 1MB)
command -v openssl >/dev/null 2>&1 || apk add --progress openssl || { echo "openssl 安装失败,请检查网络后重新运行这条命令"; exit 1; }

# 换掉旧版"热点代理"(hotspot_proxy.py),设置(端口)保留
pkill -f hotspot_proxy.py 2>/dev/null
[ -f /root/.hotspot_proxy.json ] && [ ! -f /root/.pocketproxy.json ] && mv /root/.hotspot_proxy.json /root/.pocketproxy.json
rm -f /etc/profile.d/hotspot-proxy.sh /root/hotspot_proxy.py /root/hotspot_proxy.log /root/.hotspot_proxy.json


running=$(curl -s -m 3 http://127.0.0.1:8090/api/status 2>/dev/null | sed -n 's/.*"version": *"\([0-9.]*\)".*/\1/p')
case "$running" in
  ""|0.*|1.0.[0-4])
    echo "[2/3] 下载口袋代理"
    ok=
    for src in 1 2 3; do
      if get $src pocketproxy.py /root/pocketproxy.py.new && get $src pocketproxy.py.sig /root/pocketproxy.py.sig; then
        verify /root/pocketproxy.py.new /root/pocketproxy.py.sig && ok=1 && break
        echo "  第 $src 个下载地址的文件没通过签名校验,换下一个"
      else
        echo "  第 $src 个下载地址下载失败,换下一个"
      fi
    done
    rm -f /root/pocketproxy.py.sig
    [ -n "$ok" ] || { rm -f /root/pocketproxy.py.new; echo "下载失败或签名不对,没有安装。请检查网络后重新运行这条命令"; exit 1; }
    # 先停旧版,再检查新文件:不让两个 Python 同时跑(iSH 内存紧,同时跑两个时 iSH 曾自动退出)
    if [ -n "$running" ]; then
      pkill -f "python3 -u /root/pocketproxy.py" 2>/dev/null
      mark "安装命令停止了旧版 $running"
      sleep 2
    fi
    python3 -c "compile(open('/root/pocketproxy.py.new', encoding='utf-8').read(), 'p', 'exec')" 2>/dev/null \
      || { echo "下载的文件不完整,请重新运行这条命令"; rm -f /root/pocketproxy.py.new; exit 1; }
    mv /root/pocketproxy.py.new /root/pocketproxy.py
    grep -m1 "^VERSION" /root/pocketproxy.py
    mkdir -p /etc/profile.d
    python3 /root/pocketproxy.py --write-profile  # 开机脚本由程序自己维护(自动升级也会更新)
    echo "[3/3] 启动"
    . /etc/profile.d/pocketproxy.sh
    ;;
  *)
    # 1.0.5 起:让正在运行的程序自己下载新版、在原进程里换代码,不结束进程
    echo "[2/3] 正在运行的是 $running,让它自己升级(不停止进程)"
    curl -s -m 180 -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8090/api/update; echo
    echo "[3/3] 等新版就绪"
    sleep 3
    i=0
    while [ $i -lt 60 ] && ! curl -s -m 3 http://127.0.0.1:8090/api/status >/dev/null 2>&1; do sleep 1; i=$((i + 1)); done
    curl -s -m 5 http://127.0.0.1:8090/api/info || echo "新版没有响应,请关掉 iSH 再打开"
    ;;
esac
mark "安装命令结束"
