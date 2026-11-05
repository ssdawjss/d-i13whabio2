# -*- coding: utf-8 -*-
"""口袋代理 PocketProxy:把 iPhone 的网络(含 VPN)分享给电脑。在 iSH 里运行,Safari 打开 http://127.0.0.1:8090 操作。

页面上显示电脑要填的代理地址和端口,端口可改(默认 8080),按钮启动、停止,页面上可一键升级。
同一端口支持 HTTP(含 CONNECT)和 SOCKS5,只用标准库。出站连接走手机的默认路由,VPN 连接时就是 VPN 通道。
只接受局域网和本机的连接。电脑连 iPhone 热点,或和 iPhone 在同一个 Wi-Fi 里都可以,页面会自动给出地址。
电脑浏览器直接打开 http://地址:端口/ 看运行状态,/log 看最近记录。版本记录见 CHANGELOG.md。
"""
import collections
import json
import os
import random
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = '1.0.9'
NAME = '口袋代理 PocketProxy'
UPDATE_BASE = 'https://icatdog.dynv6.net/rpa-update/pocketproxy/'
SERVER_PIN = 'icatdog.dynv6.net:443:47.76.241.196'  # 服务器固定地址,优先用它,不依赖域名解析
GITHUB_BASE = 'https://raw.githubusercontent.com/kdwang-icatog/d-i13whabio2/main/'  # 备用下载地址(只放发布文件)
# 下载地址按顺序试:香港固定地址 → 香港按域名 → GitHub。同一次升级的程序和签名必须来自同一个地址
SOURCES = [(UPDATE_BASE, ['--resolve', SERVER_PIN]), (UPDATE_BASE, []), (GITHUB_BASE, [])]
# 发布签名公钥(ed25519,只有 base64 那一行)。新版必须用对应私钥签过名才会装,下载地址被人改了也装不上假版本。
# 由 keygen.sh 写入;为空时拒绝升级(publish.sh 也会拒绝发布)
SIGN_PUBKEY = 'MCowBQYDK2VwAyEA9O1YjAhlzFrGr0Mfn++Zs2nlNr6GsruY2qZ19oijMhE='
FEEDBACK_EMAIL = 'pocket-feedback.ride078@passinbox.com'  # 问题报告收件地址
DONATE_URL = ''  # 打赏页面,为空就不显示
UI_PORT = 8090
HERE = os.path.abspath(__file__)
BAK = HERE + '.bak'
UI_SRV = [None]
RETIRED = [False]  # 已被新版替换:旧代码的后台线程见到就退出
STARTED_OK = [False]  # 端口已打开;升级时旧代码据此判断新版有没有起来
CONF = os.path.expanduser('~/.pocketproxy.json')
HOTSPOT_IP = '172.20.10.1'  # iPhone 个人热点的地址固定是这个
# 只接受局域网(私有地址)和本机的连接
ALLOWED = ('127.0.0.1', '10.', '192.168.') + tuple(f'172.{i}.' for i in range(16, 32))
# 常见 VPN 软件的虚拟网卡地址,电脑连不到,不显示
VPN_TUN = ('172.16.0.', '172.19.0.', '198.18.', '198.19.', '100.64.')  # 172.16.0.1 是 V2VPN 的
# 这些网卡电脑连不到:VPN 虚拟网卡(utun/ipsec/ppp)、蜂窝(pdp_ip)、本机回环
BAD_IF = ('utun', 'ipsec', 'ppp', 'pdp_ip', 'tun', 'lo')
TIMEOUT = 15
BUF = 65536
# iSH 是模拟器,iOS 给它的内存很紧,用多了整个 iSH 会被 iOS 关掉(11-04 电脑全部流量走代理时 30 分钟内被关两次)
MAX_CLIENTS = 64  # 同时处理的电脑端连接上限
IDLE = 120  # 连接空闲这么多秒就关掉(浏览器会自动重连),原来 300 秒
THREAD_STACK = 256 * 1024  # 每个连接一个线程,默认栈 8MB,改小省内存
BAD_GATEWAY = ('HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain; charset=utf-8\r\nConnection: close\r\n\r\n'
               '口袋代理:iPhone 连不上这个网站,请检查 iPhone 的 VPN 和网络\n').encode('utf-8')

# 最近记录:每个连接一行,只在内存里(远程用 /log 看);启动、停止、出错等事件同时打印到日志文件
LOG = collections.deque(maxlen=300)


def now(fmt='%m-%d %H:%M:%S', t=None):
    """本地时间。iSH 不知道 iPhone 的时区(总是 UTC),用 Safari 页面报来的时区;没报过就按北京时间。"""
    t = time.time() if t is None else t
    off = CFG.get('tz', -480 if time.timezone == 0 else None)  # 分钟,同 JS getTimezoneOffset
    return time.strftime(fmt, time.localtime(t)) if off is None else time.strftime(fmt, time.gmtime(t - off * 60))


def log(msg, event=False):
    line = f'{now()} {msg}'
    LOG.append(line)
    if event:
        print(line, flush=True)


def load_conf():
    # 默认一打开就启动代理,不用先去 Safari 点按钮
    c = {'port': 8080, 'running': True}
    for path in (CONF, os.path.expanduser('~/.hotspot_proxy.json')):  # 兼容旧版"热点代理"的设置
        try:
            with open(path, encoding='utf-8') as f:
                return {**c, **json.load(f)}
        except (OSError, ValueError):
            continue
    return c


def save_conf(c):
    with open(CONF, 'w', encoding='utf-8') as f:
        json.dump(c, f)


def route_ip(target):
    """系统发往 target 时用的本机地址(UDP connect 不发包,只查路由)。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target, 9))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


IFCONFIG_RAW = ['']  # 最近一次 ifconfig 原始输出,远程排查用(/log 里能看到)


def ifconfig_ips():
    """按网卡名分好坏:返回 (可用地址, 不可用地址)。iPhone 上 Wi-Fi 是 en0,热点是 bridge100。"""
    try:
        r = subprocess.run(['ifconfig'], capture_output=True, text=True, timeout=3)
        out = r.stdout
        IFCONFIG_RAW[0] = (out + r.stderr).strip()[:1500]
    except (OSError, subprocess.SubprocessError) as e:
        IFCONFIG_RAW[0] = f'ifconfig 运行失败:{e}'
        return [], []
    good, bad, name = [], [], ''
    for line in out.splitlines():
        if line and not line[0].isspace():
            name = line.split()[0].rstrip(':')
        for ip in re.findall(r'inet (?:addr:)?(\d+\.\d+\.\d+\.\d+)', line):
            (bad if name.startswith(BAD_IF) else good).append(ip)
    return good, bad


_ips_cache = [0, []]
ROUTE_RAW = ['']


def local_ips():
    """电脑能连到的本机地址,热点优先,其次无线局域网。返回 [{ip, kind}]。
    iSH 里起子进程、查主机名都很慢,结果缓存 10 秒。"""
    if time.time() - _ips_cache[0] < 10:
        return _ips_cache[1]
    cands = [HOTSPOT_IP]
    routes = {t: route_ip(t) for t in ('192.168.1.1', '10.0.0.1', '172.16.0.1')}
    ROUTE_RAW[0] = ' '.join(f'{t}→{ip}' for t, ip in routes.items())
    cands += [ip for t, ip in routes.items() if ip != t]  # 查到的地址等于目标本身说明不可信
    good, bad = ifconfig_ips()
    cands += good
    try:
        cands += [i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    ips = []
    for ip in dict.fromkeys(c for c in cands if c):
        if not ip.startswith(ALLOWED) or ip.startswith('127.') or ip.startswith(VPN_TUN) or ip in bad:
            continue
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind((ip, 0))
            s.close()
        except OSError:
            continue
        ips.append({'ip': ip, 'kind': '个人热点' if ip == HOTSPOT_IP else '无线局域网'})
    ips.sort(key=lambda x: x['kind'] != '个人热点')
    _ips_cache[:] = [time.time(), ips]
    return ips


def recv_exact(s, n):
    data = b''
    while len(data) < n:
        c = s.recv(n - len(data))
        if not c:
            raise OSError('连接提前关闭')
        data += c
    return data


class Proxy:
    def __init__(self):
        self.lock = threading.Lock()
        self.srv = None
        self.port = None
        self.conns = set()
        self.sent = self.recv = self.total = self.failed = 0
        self.clients = set()  # 正在处理的电脑端连接
        self.last_full = 0
        self.started = None
        self.last_error = ''

    @property
    def running(self):
        return self.srv is not None

    def start(self, port):
        with self.lock:
            if self.srv:
                self.stop_locked()
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(('0.0.0.0', port))
            except OSError as e:
                s.close()
                raise OSError(f'端口 {port} 用不了({e.strerror or e}),换一个试试')
            s.listen(64)
            s.settimeout(1)
            self.srv, self.port, self.started = s, port, time.time()
            self.sent = self.recv = self.total = self.failed = 0
            threading.Thread(target=self.accept_loop, args=(s,), daemon=True).start()
        log(f'代理已启动,端口 {port}', True)

    def stop(self):
        with self.lock:
            if self.srv:
                log('代理已停止', True)
            self.stop_locked()

    def stop_locked(self):
        if self.srv:
            self.srv.close()
            self.srv = None
        for c in list(self.conns):
            try:
                c.close()
            except OSError:
                pass
        self.conns.clear()

    def accept_loop(self, s):
        while self.srv is s:
            try:
                c, addr = s.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not addr[0].startswith(ALLOWED):
                log(f'拒绝了非局域网来源 {addr[0]}', True)
                c.close()
                continue
            if len(self.clients) >= MAX_CLIENTS:  # 连接太多:拒绝新的,免得 iSH 内存涨到被 iOS 关掉
                c.close()
                if time.time() - self.last_full > 60:
                    self.last_full = time.time()
                    log(f'同时连接已达上限 {MAX_CLIENTS},暂时拒绝新连接', True)
                continue
            self.total += 1
            threading.Thread(target=self.handle, args=(c, addr), daemon=True).start()

    def relay(self, a, b, info):
        a.settimeout(None)
        b.settimeout(None)
        try:
            while True:
                ready, _, _ = select.select([a, b], [], [], IDLE)
                if not ready:
                    return
                for s in ready:
                    data = s.recv(BUF)
                    if not data:
                        return
                    (b if s is a else a).sendall(data)
                    if s is a:
                        self.sent += len(data)
                        info['up'] += len(data)
                    else:
                        self.recv += len(data)
                        info['down'] += len(data)
        except (OSError, ValueError):
            pass

    def connect(self, host, port, info):
        info['target'] = f'{host}:{port}'
        r = socket.create_connection((host, port), TIMEOUT)
        self.conns.add(r)
        return r

    def drop(self, r):
        self.conns.discard(r)
        r.close()

    def socks5(self, c, info):
        info['kind'] = 'SOCKS5'
        n = recv_exact(c, 1)[0]
        recv_exact(c, n)
        c.sendall(b'\x05\x00')
        ver, cmd, _, atyp = recv_exact(c, 4)
        if atyp == 1:
            host = socket.inet_ntoa(recv_exact(c, 4))
        elif atyp == 3:
            host = recv_exact(c, recv_exact(c, 1)[0]).decode()
        elif atyp == 4:
            host = socket.inet_ntop(socket.AF_INET6, recv_exact(c, 16))
        else:
            raise OSError('地址类型不支持')
        port = struct.unpack('>H', recv_exact(c, 2))[0]
        if cmd != 1:
            c.sendall(b'\x05\x07\x00\x01' + b'\0' * 6)
            return
        try:
            r = self.connect(host, port, info)
        except OSError:
            c.sendall(b'\x05\x05\x00\x01' + b'\0' * 6)
            raise
        c.sendall(b'\x05\x00\x00\x01' + b'\0' * 6)
        self.relay(c, r, info)
        self.drop(r)

    def http(self, c, first, info):
        info['kind'] = 'HTTP'
        head = first
        while b'\r\n\r\n' not in head and len(head) < 65536:
            d = c.recv(BUF)
            if not d:
                return
            head += d
        line, _, rest = head.partition(b'\r\n')
        method, target, ver = line.decode('latin1').split(' ', 2)
        if target.startswith('/'):
            # 直接用浏览器打开 http://地址:端口/ 显示运行状态,/log 显示最近记录,方便在电脑上确认和排查
            info['kind'] = '查看'
            info['target'] = target[:40]
            body = (log_text() if target.startswith('/log') else info_text()).encode('utf-8')
            c.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: '
                      + str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n' + body)
            return
        if method == 'CONNECT':
            host, _, port = target.rpartition(':')
            try:
                r = self.connect(host.strip('[]'), int(port), info)
            except OSError:
                c.sendall(BAD_GATEWAY)
                raise
            c.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            self.relay(c, r, info)
            self.drop(r)
            return
        if not target.startswith('http://'):
            c.sendall(b'HTTP/1.1 400 Bad Request\r\n\r\n')
            return
        hostport, _, path = target[7:].partition('/')
        host, _, port = hostport.partition(':')
        try:
            r = self.connect(host, int(port or 80), info)
        except OSError:
            c.sendall(BAD_GATEWAY)
            raise
        req = f'{method} /{path} {ver}\r\n'.encode('latin1') + rest
        r.sendall(req)
        self.sent += len(req)
        info['up'] += len(req)
        self.relay(c, r, info)
        self.drop(r)

    def handle(self, c, addr):
        self.conns.add(c)
        self.clients.add(c)
        info = {'kind': '?', 'target': '-', 'up': 0, 'down': 0}
        t0 = time.time()
        err = ''
        try:
            c.settimeout(TIMEOUT)
            first = c.recv(1)  # 不用 MSG_PEEK,iSH 不一定支持
            if first == b'\x05':
                self.socks5(c, info)
            elif first:
                self.http(c, first, info)
        except Exception as e:  # noqa: BLE001
            err = f'{type(e).__name__} {str(e)[:60]}'
            self.failed += 1
            self.last_error = f'{now("%H:%M:%S")} {addr[0]} → {info["target"]} {err}'
        finally:
            self.conns.discard(c)
            self.clients.discard(c)
            c.close()
        log(f'{addr[0]} {info["kind"]} {info["target"]} 上传 {fmt(info["up"])} 下载 {fmt(info["down"])} '
            f'{time.time() - t0:.1f}秒' + (f' 失败:{err}' if err else ''))

    def status(self):
        return {'running': self.running, 'port': self.port, 'ips': local_ips(), 'active': len(self.clients),
                'total': self.total, 'failed': self.failed, 'sent': self.sent, 'recv': self.recv,
                'uptime': int(time.time() - self.started) if self.running else 0, 'lastError': self.last_error}


def fmt(n):
    for unit in ('B', 'KB', 'MB'):
        if n < 1024:
            return f'{n:.0f}{unit}' if unit == 'B' else f'{n:.1f}{unit}'
        n /= 1024
    return f'{n:.2f}GB'


PROXY = Proxy()
CFG = load_conf()
STARTED = time.time()


def info_text():
    s = PROXY.status()
    addr = '、'.join(f"{x['ip']}({x['kind']})" for x in s['ips']) or '没找到网络'
    state = f"运行中,端口 {s['port']}" if s['running'] else '已停止(Safari 打开 http://127.0.0.1:8090 点启动)'
    up = UPDATE['latest'] if newer(UPDATE['latest'], VERSION) else ''
    return (f"{NAME} {VERSION}" + (f"(有新版本 {up})" if up else '') + f"\n代理:{state}\n电脑代理地址:{addr}\n"
            f"当前连接 {s['active']},累计 {s['total']},失败 {s['failed']},上传 {fmt(s['sent'])},下载 {fmt(s['recv'])}\n"
            f"程序已运行 {int(time.time() - STARTED) // 60} 分钟;最近错误:{s['lastError'] or '无'}\n"
            f"定位保活:{location_text()}\n")


def log_text():
    return (info_text() + f'最近心跳:{LAST_BEAT[0] or "还没有"}\n'
            + f'\n网络诊断:路由 {ROUTE_RAW[0]}\nifconfig:\n{IFCONFIG_RAW[0]}\n'
            + '\n最近记录(新的在下面):\n' + '\n'.join(LOG) + '\n')


# ---------- 问题报告 ----------
PRIVATE_IP = re.compile(r'(127\.|10\.|192\.168\.|169\.254\.|0\.0\.0\.0|255\.|172\.(1[6-9]|2\d|3[01])\.)')
FILE_EXT = ('.py', '.sh', '.json', '.log', '.txt', '.sig', '.pem', '.pid')


def redact(text):
    """去掉报告里用户访问过的网站:连接目标只留端口,其余域名、公网地址、IPv6、网卡硬件地址换成 *。"""
    text = re.sub(r'(SOCKS5|HTTP) (\S+):(\d+) ', r'\1 *:\3 ', text)
    text = re.sub(r'→ (\S+):(\d+)', r'→ *:\2', text)
    text = re.sub(r'inet6 (?:addr: ?)?\S+', 'inet6 *', text)
    text = re.sub(r'\b[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}\b', '*', text)
    text = re.sub(r'\b(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b',
                  lambda m: m.group(0) if m.group(0).lower().endswith(FILE_EXT) else '*', text)
    return re.sub(r'(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])',
                  lambda m: m.group(0) if PRIVATE_IP.match(m.group(0)) else '*', text)


def build_report():
    """问题报告:重要的在前(邮件可能只带得下前面一部分)。不含访问过的网站,只在用户点了才生成,不自动上传。"""
    code = ''.join(random.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(6))
    text = (f'问题描述(请在这里写一句发生了什么):\n\n\n'
            f'---- 以下由程序生成,已去掉访问过的网站 ----\n反馈码 {code},生成时间 {now("%Y-%m-%d %H:%M:%S")}\n'
            + info_text() + f'最近心跳:{LAST_BEAT[0] or "还没有"}\n内存 {mem_mb()}MB,线程 {threading.active_count()}\n'
            + '\n最近记录(新的在下面):\n' + '\n'.join(list(LOG)[-40:])
            + f'\n\n网络诊断:路由 {ROUTE_RAW[0]}\nifconfig:\n{IFCONFIG_RAW[0]}\n')
    return {'code': code, 'text': redact(text), 'email': FEEDBACK_EMAIL,
            'subject': f'[口袋代理反馈] {code} {VERSION}'}


# ---------- 升级 ----------
UPDATE = {'latest': '', 'checked': 0, 'error': '', 'busy': False}


def newer(a, b):
    try:
        return tuple(map(int, a.split('.'))) > tuple(map(int, b.split('.')))
    except (AttributeError, ValueError):
        return False


def fetch_from(src, name, dest=None):
    """从一个下载地址用 curl 下载(iSH 里比 Python 自带的 HTTPS 更可靠)。返回 (成功, 内容或错误)。"""
    base, extra = src
    cmd = ['curl', '-fsSL', '-m', '60', *extra, base + name] + (['-o', dest] if dest else [])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90)
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return (True, r.stdout) if r.returncode == 0 else (False, r.stderr.strip() or f'curl 退出码 {r.returncode}')


def fetch(name, dest=None):
    """按 SOURCES 的顺序试,哪个先成功用哪个。国内解析域名常失败,所以先连香港固定地址(证书照常校验)。"""
    err = '没有下载地址'
    for src in SOURCES:
        ok, out = fetch_from(src, name, dest)
        if ok:
            return True, out
        err = out
    return False, err


def ensure_openssl():
    """校验签名要用 openssl 命令(iSH 默认没有,python3 已经带了它的库,只差约 1MB 的命令本身)。"""
    if shutil.which('openssl'):
        return True
    try:
        subprocess.run(['apk', 'add', '--no-progress', 'openssl'], capture_output=True, timeout=900)
    except (OSError, subprocess.SubprocessError):
        pass
    return bool(shutil.which('openssl'))


def verify_sig(path, sig):
    """用内置公钥校验 path 的签名。返回错误文字,通过返回空。"""
    if not SIGN_PUBKEY:
        return '这个版本没有内置签名公钥,不能自动升级,请重新运行安装命令'
    if not ensure_openssl():
        return '缺少 openssl,无法校验新版签名,没有升级(请在 iSH 里运行 apk add openssl)'
    pub = HERE + '.pub'
    with open(pub, 'w') as f:
        f.write(f'-----BEGIN PUBLIC KEY-----\n{SIGN_PUBKEY}\n-----END PUBLIC KEY-----\n')
    try:
        r = subprocess.run(['openssl', 'pkeyutl', '-verify', '-pubin', '-inkey', pub, '-rawin', '-in', path,
                            '-sigfile', sig], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as e:
        return f'校验签名出错:{e}'
    finally:
        os.remove(pub)
    return '' if r.returncode == 0 else '新版签名不对,可能被人改过,没有升级'


def fetch_signed(dest):
    """下载新版程序和它的签名(同一个地址),校验通过才算成功。返回错误文字,成功返回空。"""
    sig, err = dest + '.sig', '没有下载地址'
    for src in SOURCES:
        ok, out = fetch_from(src, 'pocketproxy.py', dest)
        if ok:
            ok, out = fetch_from(src, 'pocketproxy.py.sig', sig)
        if not ok:
            err = f'下载失败:{out[:80]}'
            continue
        err = verify_sig(dest, sig)
        os.remove(sig)
        if not err:
            return ''
        log(f'{src[0]} 的新版没通过签名校验', True)
    for f in (dest, sig):
        if os.path.exists(f):
            os.remove(f)
    return err


def check_update():
    ok, out = fetch('version.txt')
    UPDATE['checked'] = time.time()
    if ok and re.fullmatch(r'\d+(\.\d+)*', out.strip()):
        UPDATE['latest'], UPDATE['error'] = out.strip(), ''
    else:
        UPDATE['error'] = f'检查更新失败:{out[:80]}'


def update_loop():
    """每小时查一次新版本;有新版本就自动升级,但要等没有连接时(最多等 1 小时,不打断正在用的网络)。"""
    while not RETIRED[0]:  # 升级后旧代码的这个线程醒来就退出
        check_update()
        auto = newer(UPDATE['latest'], VERSION) and UPDATE['latest'] != CFG.get('skip')
        for _ in range(12 if auto else 0):
            if RETIRED[0]:
                return
            if not PROXY.conns and not UPDATE['busy']:
                UPDATE['busy'] = True
                log(f'发现新版本 {UPDATE["latest"]},自动升级', True)
                err = do_update()
                if err:
                    UPDATE['busy'] = False
                    log(err, True)
                break
            time.sleep(300)
        time.sleep(3600)


DOC_CACHE = {}


def doc_text(name):
    """使用说明、更新记录:从服务器取最新的,标明 UTF-8 给 Safari(直接打开服务器上的文件会乱码)。"""
    ok, out = fetch(name)
    if ok:
        DOC_CACHE[name] = out.lstrip('﻿')
    return DOC_CACHE.get(name) or f'暂时取不到{"更新记录" if "CHANGE" in name else "使用说明"},请检查网络后再试。\n'


def maybe_check_update():
    """打开界面时,距上次检查超过 10 分钟就在后台再查一次,新版本能尽快出现。"""
    if time.time() - UPDATE['checked'] > 600 and not UPDATE['busy']:
        UPDATE['checked'] = time.time()
        threading.Thread(target=check_update, daemon=True).start()


def do_update():
    """下载新版,检查能编译,替换后原地重启。返回错误文字,成功返回空。"""
    new = HERE + '.new'
    err = fetch_signed(new)
    if err:
        return err
    try:
        with open(new, encoding='utf-8') as f:
            src = f.read()
        compile(src, new, 'exec')
        ver = re.search(r"^VERSION = '([\d.]+)'", src, re.M).group(1)
    except (OSError, SyntaxError, AttributeError) as e:
        os.remove(new)
        return f'下载的文件不完整,没有替换({type(e).__name__})'
    if not newer(ver, VERSION):  # 不装旧版本:防止有人拿签过名的老版本(可能有已修的问题)来"降级"
        os.remove(new)
        return f'已是最新版本({VERSION})'
    shutil.copy2(HERE, BAK)  # 留一份旧版,新版起不来时换回
    os.replace(new, HERE)
    UPDATE['pending'] = ver
    log(f'升级 {VERSION} → {ver},正在重启', True)
    t = threading.Timer(1, restart)
    t.daemon = False  # 从后台线程发起时也不能随主程序一起结束,否则新版没起来、旧版也没了
    t.start()
    return ''


def restart():
    """换成新版第一步:放开两个端口,让主线程的 serve_forever 返回,由主线程在同一个进程里载入新代码(reload_new)。
    不结束进程、不另起进程:iSH 里 execv(1.0.1→1.0.2)和安装时两个 Python 同时跑(1.0.2→1.0.4)都让 iSH 自动退出过。"""
    RETIRED[0] = True
    PROXY.stop()
    if UI_SRV[0]:
        UI_SRV[0].shutdown()
        UI_SRV[0].server_close()


def reload_new():
    """在本进程里运行新版;新版启动失败就换回旧版再运行,并且以后不再自动升级到那个版本。"""
    import runpy
    for attempt in ('新版', '旧版'):
        os.environ['POCKETPROXY_RELOAD'] = '1'
        try:
            g = runpy.run_path(HERE, run_name='__main__')  # 新版的 main() 会一直运行下去
            if g.get('STARTED_OK', [False])[0]:
                return
            err = '端口没能打开'
        except BaseException as e:  # noqa: BLE001  新版里 sys.exit 也要接住,不能让整个进程退出
            if isinstance(e, KeyboardInterrupt):
                raise
            err = f'{type(e).__name__} {str(e)[:80]}'
        if attempt == '新版' and os.path.exists(BAK):
            os.replace(BAK, HERE)
            CFG['skip'] = UPDATE.get('pending', '')  # 这个版本不再自动升级,免得反复升级、反复换回
            save_conf(CFG)
            log(f'新版 {CFG["skip"]} 起不来({err}),已换回旧版,以后不再自动升级到这个版本', True)
        else:
            log(f'起不来:{err}', True)
            return


PAGE = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-title" content="口袋代理">
<title>口袋代理</title><style>
:root{--bg:#f2f2f7;--card:#fff;--text:#1c1c1e;--sub:#6e6e73;--line:#d1d1d6;--on:#34c759;--off:#ff3b30;--btn:#007aff}
@media (prefers-color-scheme:dark){:root{--bg:#000;--card:#1c1c1e;--text:#f2f2f7;--sub:#98989f;--line:#38383a;--btn:#0a84ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px/1.4 -apple-system,system-ui,sans-serif;padding:16px 16px 40px}
h1{font-size:28px;margin:8px 4px 2px}.ver{color:var(--sub);font-size:13px;margin:0 4px 14px}.card{background:var(--card);border-radius:14px;padding:16px;margin-bottom:14px}
.label{color:var(--sub);font-size:13px;margin-bottom:6px}.big{font:600 30px/1.2 ui-monospace,Menlo,monospace;word-break:break-all;user-select:all}
.state{display:flex;align-items:center;gap:8px;font-weight:600}
.dot{width:12px;height:12px;border-radius:50%;background:var(--off)}.on .dot{background:var(--on)}
input{width:100%;font:600 24px ui-monospace,Menlo,monospace;padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--text)}
button{width:100%;padding:16px;font-size:19px;font-weight:600;border:0;border-radius:14px;color:#fff;background:var(--btn)}
button.stop{background:var(--off)}button.small{padding:10px;font-size:16px;margin-top:10px}button:disabled{opacity:.5}.err{color:var(--off);font-size:14px;margin-top:8px;min-height:1em}
.stats{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;font-size:15px}.stats b{display:block;font-size:20px}
ol{margin:0;padding-left:20px;color:var(--sub);font-size:14px}code{font-family:ui-monospace,Menlo,monospace;color:var(--text)}
pre{margin:0;font:12px/1.45 ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-all;color:var(--sub);max-height:260px;overflow:auto}
a{color:var(--btn)}
textarea{width:100%;margin-top:8px;font:12px/1.45 ui-monospace,Menlo,monospace;padding:8px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--text)}
button.light{background:var(--bg);color:var(--btn);border:1px solid var(--line)}
</style></head><body>
<h1>口袋代理</h1><div class="ver">PocketProxy <span id="ver"></span></div>
<div class="card"><div id="state" class="state"><span class="dot"></span><span id="stateText">读取中</span></div><div class="label" id="loc" style="margin:8px 0 0"></div></div>
<div class="card"><div class="label">电脑上填的代理地址 <span id="kind"></span></div><div class="big" id="ip">—</div><div class="label" id="more" style="margin-top:6px"></div>
<label class="label" for="port" style="display:block;margin-top:12px">端口</label><input id="port" type="number" inputmode="numeric" min="1024" max="65535">
<div class="err" id="err"></div></div>
<button id="btn" onclick="toggle()">启动</button>
<div class="card" style="margin-top:14px"><div class="stats">
<div>当前连接<b id="active">0</b></div><div>累计连接<b id="total">0</b></div>
<div>上传<b id="sent">0</b></div><div>下载<b id="recv">0</b></div></div></div>
<div class="card"><div class="label">版本与升级</div><div id="upd">—</div>
<button id="ubtn" class="small" style="display:none" onclick="upgrade()">升级到新版本</button>
<div class="label" style="margin-top:8px"><a href="/doc/changelog">更新记录</a> · <a href="/doc/readme">使用说明</a> · 有新版本会在空闲时自动升级</div></div>
<div class="card"><div class="label">遇到问题</div>
<button class="small light" onclick="report()">生成问题报告</button>
<div id="rep" style="display:none"><div class="label" style="margin-top:10px">反馈码 <b id="rcode"></b>。报告里没有你访问过的网站。先在最上面写一句发生了什么,再选一种方式发给作者:</div>
<textarea id="rtext" rows="9"></textarea>
<button class="small" onclick="copyRep()">复制报告(发微信或留言)</button>
<button class="small light" onclick="mailRep()">用邮件发送</button>
<div class="label" id="rmsg" style="margin-top:6px"></div></div>
<div class="label" id="donate" style="margin-top:10px;display:none">觉得好用?<a id="dlink" href="#">支持一下作者</a></div></div>
<div class="card"><div class="label">电脑怎么设置</div><ol>
<li>启动顺序:iPhone 先连好 VPN → 打开 iSH(口袋代理自动启动)→ 电脑连 iPhone 的"个人热点",或和 iPhone 连同一个 Wi-Fi → 电脑设代理。</li>
<li>电脑上可以先 <code>ping <span class="ipv">172.20.10.1</span></code> 看看通不通,或用浏览器打开 <code>http://<span class="ipv">172.20.10.1</span>:<span class="portv">8080</span>/</code> 看运行状态。</li>
<li>Windows:设置 → 网络和 Internet → 代理 → 手动设置代理,地址填 <code class="ipv">172.20.10.1</code>,端口填 <code class="portv">8080</code>。</li>
<li>Mac:系统设置 → 网络 → 当前网络 → 详细信息 → 代理,勾选"网页代理(HTTP)"和"安全网页代理(HTTPS)",填同样的地址和端口。</li>
<li>这个端口同时支持 SOCKS5,别的软件也可以用。</li>
<li>iSH 可以切到后台,但不要从多任务里把它划掉。</li></ol></div>
<div class="card"><div class="label">最近记录</div><pre id="log"></pre></div>
<script>
let st={};
const fmt=n=>n<1024?n+' B':n<1048576?(n/1024).toFixed(1)+' KB':n<1073741824?(n/1048576).toFixed(1)+' MB':(n/1073741824).toFixed(2)+' GB';
const $=id=>document.getElementById(id);
async function api(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body&&JSON.stringify(body)});return r.json()}
function show(s){st=s;const ip=(s.ips[0]||{}).ip||'';
 $('ip').textContent=ip||'没找到网络';
 $('kind').textContent=s.ips.length?'('+s.ips[0].kind+')':'请打开个人热点,或连上 Wi-Fi';
 $('more').textContent=s.ips.length>1?'也可以用:'+s.ips.slice(1).map(x=>x.ip+'('+x.kind+')').join('、'):'';
 document.querySelectorAll('.ipv').forEach(e=>e.textContent=ip||'172.20.10.1');
 if(document.activeElement!==$('port'))$('port').value=s.port||s.savedPort;
 document.querySelectorAll('.portv').forEach(e=>e.textContent=$('port').value);
 $('state').className='state'+(s.running?' on':'');
 $('loc').textContent='定位保活:'+(s.location||'');
 $('stateText').textContent=s.running?'运行中 · 已运行 '+Math.floor(s.uptime/60)+' 分钟':'已停止';
 $('btn').textContent=s.running?'停止':'启动';$('btn').className=s.running?'stop':'';
 $('port').disabled=s.running;
 $('active').textContent=s.active;$('total').textContent=s.total;$('sent').textContent=fmt(s.sent);$('recv').textContent=fmt(s.recv);
 $('ver').textContent=s.version;const u=s.update;
 $('upd').textContent=u.busy?'正在升级,完成后页面会自动刷新……':u.newer?'有新版本 '+u.latest+'(当前 '+s.version+')':u.latest?'已是最新版本':(u.error||'正在检查更新……');
 $('ubtn').style.display=u.newer&&!u.busy?'block':'none';
 $('log').textContent=(s.log||[]).slice(-40).reverse().join('\n');
 if(s.donate){$('dlink').href=s.donate;$('donate').style.display='block'}}
async function refresh(){try{show(await api('/api/status?tz='+new Date().getTimezoneOffset()))}catch(e){$('stateText').textContent='连不上 iSH,请先打开 iSH'}}
async function toggle(){$('btn').disabled=true;$('err').textContent='';
 try{const r=st.running?await api('/api/stop',{}):await api('/api/start',{port:+$('port').value});
  if(r.error)$('err').textContent=r.error;if(r.status)show(r.status)}catch(e){$('err').textContent='连不上 iSH,请先打开 iSH'}
 $('btn').disabled=false}
async function upgrade(){$('ubtn').disabled=true;$('upd').textContent='正在下载新版本……';
 try{const r=await api('/api/update',{});if(r.error){$('upd').textContent=r.error;$('ubtn').disabled=false;return}
  $('upd').textContent='升级成功,正在重启……';setTimeout(()=>location.reload(),8000)}catch(e){$('upd').textContent='连不上 iSH,请先打开 iSH';$('ubtn').disabled=false}}
let rep={};
async function report(){$('rmsg').textContent='';try{rep=await api('/api/report');$('rcode').textContent=rep.code;$('rtext').value=rep.text;$('rep').style.display='block'}catch(e){$('rmsg').textContent='连不上 iSH,请先打开 iSH'}}
async function copyRep(){const t=$('rtext');try{await navigator.clipboard.writeText(t.value)}catch(e){t.focus();t.select();document.execCommand('copy')}
 $('rmsg').textContent='已复制。请连同反馈码 '+rep.code+' 一起发给作者。'}
function mailRep(){const v=$('rtext').value,b=v.length>2500?v.slice(0,2500)+'\n……(太长,只带了前面一部分)':v;
 location.href='mailto:'+rep.email+'?subject='+encodeURIComponent(rep.subject)+'&body='+encodeURIComponent(b);
 $('rmsg').textContent='打不开邮件的话,请用"复制报告",发到 '+rep.email}
$('port').addEventListener('input',()=>document.querySelectorAll('.portv').forEach(e=>e.textContent=$('port').value));
refresh();setInterval(refresh,2000);
</script></body></html>'''


def full_status():
    return {**PROXY.status(), 'savedPort': CFG['port'], 'version': VERSION, 'log': list(LOG)[-40:],
            'location': location_text(), 'donate': DONATE_URL, 'update': {'latest': UPDATE['latest'], 'newer': newer(UPDATE['latest'], VERSION),
                       'error': UPDATE['error'], 'busy': UPDATE['busy']}}


class UI(BaseHTTPRequestHandler):
    def reply(self, code, body, ctype='application/json; charset=utf-8'):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, query = self.path.partition('?')
        if path == '/api/status':
            m = re.fullmatch(r'tz=(-?\d{1,4})', query)
            if m and CFG.get('tz') != int(m.group(1)):  # 记住 iPhone 的时区,日志用本地时间
                CFG['tz'] = int(m.group(1))
                save_conf(CFG)
            maybe_check_update()
            return self.reply(200, full_status())
        if path in ('/doc/readme', '/doc/changelog'):
            name = 'README.txt' if path.endswith('readme') else 'CHANGELOG.txt'
            return self.reply(200, doc_text(name).encode('utf-8'), 'text/plain; charset=utf-8')
        if self.path == '/api/info':  # iSH 开机脚本打印用
            return self.reply(200, info_text().encode('utf-8'), 'text/plain; charset=utf-8')
        if self.path == '/api/log':
            return self.reply(200, log_text().encode('utf-8'), 'text/plain; charset=utf-8')
        if self.path == '/api/report':
            return self.reply(200, build_report())
        if self.path in ('/', '/index.html'):
            return self.reply(200, PAGE.encode('utf-8'), 'text/html; charset=utf-8')
        self.reply(404, {'error': 'not found'})

    def do_POST(self):
        try:
            data = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
        except ValueError:
            data = {}
        if self.path == '/api/start':
            try:
                port = int(data.get('port') or CFG['port'])
            except (TypeError, ValueError):
                return self.reply(200, {'error': '端口要填数字', 'status': full_status()})
            if not 1024 <= port <= 65535 or port == UI_PORT:
                return self.reply(200, {'error': f'端口要在 1024–65535 之间,且不能是 {UI_PORT}', 'status': full_status()})
            try:
                PROXY.start(port)
            except OSError as e:
                log(str(e), True)
                return self.reply(200, {'error': str(e), 'status': full_status()})
            CFG.update(port=port, running=True)
            save_conf(CFG)
            return self.reply(200, {'status': full_status()})
        if self.path == '/api/stop':
            PROXY.stop()
            CFG['running'] = False
            save_conf(CFG)
            return self.reply(200, {'status': full_status()})
        if self.path == '/api/update':
            if UPDATE['busy']:
                return self.reply(200, {'error': '正在升级,请稍等'})
            UPDATE['busy'] = True
            err = do_update()
            if err:
                UPDATE['busy'] = False
                log(err, True)
            return self.reply(200, {'error': err} if err else {'ok': True})
        self.reply(404, {'error': 'not found'})

    def log_message(self, *a):
        pass


# iSH 开机脚本:每次打开 iSH 终端时运行。由程序自己保持最新(自动升级也会更新它),安装命令用 --write-profile 写入
PROFILE_PATH = '/etc/profile.d/pocketproxy.sh'
PROFILE = r'''# 口袋代理:打开 iSH 时自动启动(已在运行就跳过),并显示电脑要填的地址。由 pocketproxy.py 自动维护,别手改
# 读位置时 iOS 不会把 iSH 挂起,切到后台、锁屏后代理还能继续(首次会弹定位权限,请允许)
up() { curl -s -m 3 http://127.0.0.1:8090/api/status >/dev/null 2>&1; }
echo "@$(date +%s) 打开了 iSH 终端" >> /root/pocketproxy.log
if ! { [ -f /tmp/loc.pid ] && kill -0 "$(cat /tmp/loc.pid)" 2>/dev/null; }; then
  cat /dev/location >/dev/null 2>&1 &
  echo $! > /tmp/loc.pid
fi
if ! up; then
  # 日志超过 500KB 就换新,旧的留一份 .1
  [ -f /root/pocketproxy.log ] && [ "$(wc -c < /root/pocketproxy.log)" -gt 500000 ] && mv /root/pocketproxy.log /root/pocketproxy.log.1
  echo "口袋代理启动中,iSH 比较慢,大约要等半分钟,请先别切走……"
  nohup python3 -u /root/pocketproxy.py >>/root/pocketproxy.log 2>&1 &
  i=0
  while [ $i -lt 120 ] && ! up; do sleep 1; i=$((i + 1)); done
  # 刚升级的新版起不来:换回升级前留的旧版再启动一次
  if ! up && [ -f /root/pocketproxy.py.bak ]; then
    echo "新版起不来,换回旧版……"
    mv /root/pocketproxy.py.bak /root/pocketproxy.py
    nohup python3 -u /root/pocketproxy.py >>/root/pocketproxy.log 2>&1 &
    i=0
    while [ $i -lt 120 ] && ! up; do sleep 1; i=$((i + 1)); done
  fi
fi
if up; then
  curl -s -m 5 http://127.0.0.1:8090/api/info
  echo "界面(改端口、停止、升级):Safari 打开 http://127.0.0.1:8090。iSH 可以切到后台,但别从多任务里划掉。"
else
  echo "口袋代理没能启动,下面是错误记录(请截图):"
  tail -n 20 /root/pocketproxy.log
fi
'''


def write_profile(force=False):
    """只在 iSH 里(有 /dev/location)才写;内容没变就不动。"""
    if not (force or os.path.exists('/dev/location')) or not os.path.isdir(os.path.dirname(PROFILE_PATH)):
        return False
    try:
        with open(PROFILE_PATH, encoding='utf-8') as f:
            if f.read() == PROFILE:
                return False
    except OSError:
        pass
    with open(PROFILE_PATH, 'w', encoding='utf-8', newline='\n') as f:
        f.write(PROFILE)
    return True


def mem_mb():
    try:
        with open('/proc/self/status') as f:
            for ln in f:
                if ln.startswith('VmRSS:'):
                    return int(ln.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024  # 拿不到当前值时用峰值
    except (ImportError, OSError):
        return -1


LAST_BEAT = ['']
LOC = {'last': 0.0, 'bytes': 0, 'error': '', 'opened': 0}  # 定位保活状态


def location_loop():
    """自己读 /dev/location 保活:iOS 只有在 App 持续用定位时才让 iSH 在后台继续跑。
    读不到或断了每 30 秒重开一次;状态显示在界面和 /log 里,一眼看出定位权限是否有效。"""
    while not RETIRED[0]:
        if not os.path.exists('/dev/location'):
            LOC['error'] = '不是 iSH,没有 /dev/location'
            return
        try:
            with open('/dev/location', 'rb', buffering=0) as f:
                LOC['opened'] += 1
                while not RETIRED[0]:
                    d = f.read(4096)
                    if not d:
                        break
                    LOC['last'], LOC['bytes'], LOC['error'] = time.time(), LOC['bytes'] + len(d), ''
            LOC['error'] = '定位数据流结束了'
        except OSError as e:
            LOC['error'] = f'读不了定位({e.strerror or e})'
        time.sleep(30)


def location_text():
    if LOC['error'].startswith('不是 iSH'):
        return '不适用(不是 iSH)'
    if LOC['last'] and time.time() - LOC['last'] < 120:
        return f'正常(最近 {int(time.time() - LOC["last"])} 秒前收到定位)'
    why = LOC['error'] or '还没收到定位数据'
    return f'没有定位数据——{why}。请在 iPhone"设置 → iSH → 位置"选"始终"并打开"精确位置",否则切到后台会被暂停'


def heartbeat_loop():
    """每分钟往日志文件写一行状态。iSH 被 iOS 关掉时程序来不及留话,事后看最后几行心跳就知道当时的连接数和内存。"""
    while not RETIRED[0]:
        time.sleep(60)
        if RETIRED[0]:
            return
        loc = f'{int(time.time() - LOC["last"])}秒前' if LOC['last'] else (LOC['error'] or '无')
        LAST_BEAT[0] = (f'{now()} 心跳 连接 {len(PROXY.clients)} 线程 {threading.active_count()} 内存 {mem_mb()}MB'
                        f' 定位 {loc}')
        print(LAST_BEAT[0], flush=True)  # 只写文件,不占内存里的最近记录


def load_old_log():
    """把日志文件里最近的事件(含升级记录)带进内存,重启后远程 /log 也能看到。"""
    try:
        lines = []
        with open(os.path.expanduser('~/pocketproxy.log'), encoding='utf-8', errors='replace') as f:
            for ln in f:
                ln = ln.rstrip('\n')
                m = re.match(r'@(\d+) (.*)', ln)  # 安装脚本、iSH 开机脚本写的记录(它们只有 UTC 秒数)
                if m:
                    lines.append(f'{now(t=int(m.group(1)))} {m.group(2)}')
                elif re.match(r'\d\d-\d\d \d\d:\d\d:\d\d ', ln):
                    lines.append(ln)
        events = [ln for ln in lines if ' 心跳 ' not in ln][-40:]
        beats = [ln for ln in lines if ' 心跳 ' in ln][-5:]  # 被关掉之前最后几次心跳
        LOG.extend(sorted(events + beats, key=lambda ln: ln[:14]))
    except OSError:
        pass


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')  # 日志文件一律 UTF-8
    except (AttributeError, ValueError):
        pass
    reloaded = os.environ.pop('POCKETPROXY_RELOAD', '') == '1'  # 被旧版在同一进程里载入(升级)
    try:
        threading.stack_size(THREAD_STACK)  # 之后新开的线程都用小栈
    except (ValueError, RuntimeError):
        pass
    for i in range(10 if reloaded else 1):  # 升级时旧版刚放开端口,稍等
        try:
            ui = ThreadingHTTPServer(('127.0.0.1', UI_PORT), UI)
            break
        except OSError:
            time.sleep(1)
    else:
        print(f'已经在运行了。在 Safari 打开 http://127.0.0.1:{UI_PORT}', flush=True)
        return
    UI_SRV[0] = ui
    STARTED_OK[0] = True
    load_old_log()
    log(f'{NAME} {VERSION} 启动(进程 {os.getpid()}' + (',升级后原进程接着跑)' if reloaded else ')'), True)
    # 每次打开都启动代理;界面上的"停止"只管这一次,下次打开 iSH 照样自动启动
    try:
        PROXY.start(CFG['port'])
    except OSError as e:
        PROXY.last_error = str(e)
        log(str(e), True)
    threading.Thread(target=update_loop, daemon=True).start()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    threading.Thread(target=location_loop, daemon=True).start()
    threading.Thread(target=ensure_openssl, daemon=True).start()  # 先装好,升级时校验签名要用
    try:
        if write_profile():
            log('已更新 iSH 开机脚本', True)
    except OSError as e:
        log(f'更新 iSH 开机脚本失败:{e}', True)
    print(f'界面:Safari 打开 http://127.0.0.1:{UI_PORT}(可以"添加到主屏幕")', flush=True)
    print(info_text(), flush=True)
    ui.serve_forever()
    if RETIRED[0]:  # 只有升级会走到这里:端口已放开,在本进程里换成新版
        reload_new()


if __name__ == '__main__':
    if '--write-profile' in sys.argv:  # 安装命令用
        print(f'已写入 {PROFILE_PATH}' if write_profile(force=True) else f'{PROFILE_PATH} 已是最新或无法写入')
    else:
        main()
