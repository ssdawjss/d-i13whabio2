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
# 校验签名:通过返回 0。用 Python 算 Ed25519(iSH 里的 openssl 太老,不支持),和 pocketproxy.py 里的算法相同
verify() {
  [ -n "$PUBKEY" ] || return 1
  cat > /root/.pp_verify.py <<'PYEOF'
import base64, hashlib, sys
p = 2 ** 255 - 19
q = 2 ** 252 + 27742317777372353535851937790883648493
d = -121665 * pow(121666, p - 2, p) % p
m1 = pow(2, (p - 1) // 4, p)
def rx(y, s):
    if y >= p: return None
    x2 = (y * y - 1) * pow(d * y * y + 1, p - 2, p) % p
    if x2 == 0: return None if s else 0
    x = pow(x2, (p + 3) // 8, p)
    if (x * x - x2) % p: x = x * m1 % p
    if (x * x - x2) % p: return None
    return p - x if (x & 1) != s else x
def add(P, Q):
    a = (P[1] - P[0]) * (Q[1] - Q[0]) % p; b = (P[1] + P[0]) * (Q[1] + Q[0]) % p
    c = 2 * P[3] * Q[3] * d % p; e = 2 * P[2] * Q[2] % p
    E, F, G, H = b - a, e - c, e + c, b + a
    return E * F % p, G * H % p, F * G % p, E * H % p
def mul(s, P):
    Q = (0, 1, 1, 0)
    while s:
        if s & 1: Q = add(Q, P)
        P, s = add(P, P), s >> 1
    return Q
def pt(b):
    y = int.from_bytes(b, 'little'); s, y = y >> 255, y & ((1 << 255) - 1); x = rx(y, s)
    return None if x is None else (x, y, 1, x * y % p)
pub = base64.b64decode(sys.argv[1])[-32:]
msg = open(sys.argv[2], 'rb').read(); sig = open(sys.argv[3], 'rb').read()
A, R = pt(pub), (pt(sig[:32]) if len(sig) == 64 else None)
if A is None or R is None or int.from_bytes(sig[32:], 'little') >= q: sys.exit(1)
gy = 4 * pow(5, p - 2, p) % p; gx = rx(gy, 0)
h = int.from_bytes(hashlib.sha512(sig[:32] + pub + msg).digest(), 'little') % q
P1, P2 = mul(int.from_bytes(sig[32:], 'little'), (gx, gy, 1, gx * gy % p)), add(R, mul(h, A))
sys.exit(0 if (P1[0] * P2[2] - P2[0] * P1[2]) % p == 0 and (P1[1] * P2[2] - P2[1] * P1[2]) % p == 0 else 1)
PYEOF
  python3 /root/.pp_verify.py "$PUBKEY" "$1" "$2" >/dev/null 2>&1
  r=$?; rm -f /root/.pp_verify.py; return $r
}
# 写一条记录到日志(只有 UTC 秒数,程序显示时换成本地时间),用来事后看 iSH 有没有被重新打开过
mark() { echo "@$(date +%s) $*" >> "$LOGF"; }
ver_of() { curl -s -m 3 http://127.0.0.1:8090/api/status 2>/dev/null | sed -n 's/.*"version": *"\([0-9.]*\)".*/\1/p'; }

# 下载、校验、替换并启动(旧版在运行就先停掉)
install_fresh() {
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
}

echo "== 口袋代理 PocketProxy 安装/升级 =="
mark "运行了安装/升级命令"

if command -v python3 >/dev/null 2>&1; then
  echo "[1/3] python3 已经装好,跳过"
else
  echo "[1/3] 安装 python3(约 14MB,iSH 解压很慢,常要 5-15 分钟;下面有进度条,请等着别切走)"
  head -n 1 /etc/apk/repositories
  apk add --progress python3 || { echo "python3 安装失败,请检查网络后重新运行这条命令"; exit 1; }
fi

# 换掉旧版"热点代理"(hotspot_proxy.py),设置(端口)保留
pkill -f hotspot_proxy.py 2>/dev/null
[ -f /root/.hotspot_proxy.json ] && [ ! -f /root/.pocketproxy.json ] && mv /root/.hotspot_proxy.json /root/.pocketproxy.json
rm -f /etc/profile.d/hotspot-proxy.sh /root/hotspot_proxy.py /root/hotspot_proxy.log /root/.hotspot_proxy.json

running=$(ver_of)
case "$running" in
  ""|0.*|1.0.[0-4]|1.0.9|1.0.10)
    # 没在运行、太旧(不会原地升级),或 1.0.9/1.0.10(它们用 openssl 校验签名,在 iSH 里验不过,自己升不上去)
    install_fresh
    ;;
  *)
    # 1.0.5 起:先让正在运行的程序自己下载新版、在原进程里换代码,不结束进程
    echo "[2/3] 正在运行的是 $running,让它自己升级(不停止进程)"
    curl -s -m 180 -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:8090/api/update; echo
    echo "[3/3] 等新版就绪"
    sleep 3
    i=0
    while [ $i -lt 60 ] && [ -z "$(ver_of)" ]; do sleep 1; i=$((i + 1)); done
    now=$(ver_of)
    if [ -n "$now" ] && [ "$now" != "$running" ]; then
      curl -s -m 5 http://127.0.0.1:8090/api/info
    elif get 1 version.txt /root/.pp_latest || get 2 version.txt /root/.pp_latest || get 3 version.txt /root/.pp_latest; then
      latest=$(tr -d ' \r\n' < /root/.pp_latest); rm -f /root/.pp_latest
      if [ "$latest" = "$running" ]; then
        echo "已经是最新版本 $running"
        curl -s -m 5 http://127.0.0.1:8090/api/info
      else
        echo "它自己没升级成功,改为直接安装 $latest"
        running=${now:-$running}
        install_fresh
      fi
    else
      echo "查不到最新版本号,请检查网络后重新运行这条命令"
    fi
    ;;
esac
mark "安装命令结束"
