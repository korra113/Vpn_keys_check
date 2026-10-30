import os
import sys
import json
import base64
import urllib.parse
import asyncio
import aiohttp
from aiohttp import web
import platform
import zipfile
import urllib.request
import tempfile
import shutil
import re
import socket
from typing import Optional

# Пытаемся импортировать библиотеку для чтения баз MaxMind/GeoIP
try:
    import maxminddb
    HAS_MAXMIND = True
except ImportError:
    HAS_MAXMIND = False

HOST = "0.0.0.0"
PORT = 3000
DEFAULT_CHECK_URL = "https://cp.cloudflare.com/generate_204"
SPEEDTEST_URL = "https://speed.cloudflare.com/__down?bytes=5242880"  # 5 MB
GEOIP_DB_FILE = "GeoLite2-Country.mmdb"

SING_BOX_BIN = None
GEOIP_READER = None
_dns_cache = {}

# --- Работа с ядром sing-box ---

def get_singbox_path():
    if hasattr(sys, '_MEIPASS'):
        bundle_path = os.path.join(sys._MEIPASS, "sing-box.exe" if platform.system() == "Windows" else "sing-box")
        if os.path.exists(bundle_path):
            return bundle_path
            
    exe_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
    nearby = os.path.join(exe_dir, "sing-box.exe" if platform.system() == "Windows" else "sing-box")
    if os.path.exists(nearby):
        return nearby
        
    local = "sing-box.exe" if platform.system() == "Windows" else "./sing-box"
    if os.path.exists(local):
        return local
        
    return None

def ensure_singbox():
    global SING_BOX_BIN
    detected = get_singbox_path()
    if detected:
        SING_BOX_BIN = detected
        return

    print("[*] Ядро sing-box не найдено рядом. Скачиваем...")
    system = platform.system().lower()
    arch = platform.machine().lower()
    arch_str = "arm64" if ("arm64" in arch or "aarch64" in arch) else "amd64"
    version = "1.9.0"

    filename = f"sing-box-{version}-windows-{arch_str}.zip" if system == "windows" else f"sing-box-{version}-{system}-{arch_str}.tar.gz"

    urls = [
        f"https://github.com/SagerNet/sing-box/releases/download/v{version}/{filename}",
        f"https://ghfast.top/https://github.com/SagerNet/sing-box/releases/download/v{version}/{filename}",
        f"https://gh-proxy.com/https://github.com/SagerNet/sing-box/releases/download/v{version}/{filename}"
    ]

    tmp_archive = "singbox_tmp.archive"
    downloaded = False

    for target_url in urls:
        try:
            req = urllib.request.Request(target_url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=12) as resp, open(tmp_archive, 'wb') as out_f:
                shutil.copyfileobj(resp, out_f)
            downloaded = True
            break
        except Exception:
            continue

    if not downloaded:
        print("\n[!] ОШИБКА: Не удалось скачать sing-box автоматически.")
        print("[!] Скачайте sing-box.exe вручную и положите в одну папку с этой программой.")
        input("\nНажмите Enter, чтобы закрыть окно...")
        sys.exit(1)

    try:
        if filename.endswith(".zip"):
            with zipfile.ZipFile(tmp_archive, 'r') as z:
                for f in z.namelist():
                    if f.endswith("sing-box.exe") or f.endswith("sing-box"):
                        with z.open(f) as src, open("sing-box.exe", "wb") as dst:
                            shutil.copyfileobj(src, dst)
        print("[+] sing-box успешно установлен!")
        SING_BOX_BIN = "sing-box.exe"
    finally:
        if os.path.exists(tmp_archive):
            os.remove(tmp_archive)

    if system != "windows":
        os.chmod(SING_BOX_BIN, 0o755)

# --- Инициализация оффлайн-базы GeoIP ---

def ensure_geoip():
    global GEOIP_READER
    if not HAS_MAXMIND:
        print("[!] Библиотека 'maxminddb' не установлена. Определение стран отключено.")
        print("    Для включения выполните: pip install maxminddb")
        return

    db_paths = [
        os.path.join(getattr(sys, '_MEIPASS', ''), GEOIP_DB_FILE),  # <-- добавить эту строчку
        GEOIP_DB_FILE,
        "Country.mmdb",
        os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), GEOIP_DB_FILE),
        os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "Country.mmdb")
    ]

    found_path = next((p for p in db_paths if os.path.exists(p)), None)

    if not found_path:
        print("[*] База GeoIP (.mmdb) не найдена. Загружаем актуальную базу стран...")
        mirrors = [
            "https://github.com/P3TERX/GeoLite.mmdb/raw/download/GeoLite2-Country.mmdb",
            "https://raw.githubusercontent.com/alireza0/s-box/main/geoip.db",
            "https://github.com/Loyalsoldier/geoip/raw/release/Country.mmdb"
        ]
        downloaded = False
        for url in mirrors:
            try:
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=12) as resp, open(GEOIP_DB_FILE, 'wb') as out_f:
                    shutil.copyfileobj(resp, out_f)
                downloaded = True
                found_path = GEOIP_DB_FILE
                print("[+] База GeoIP успешно загружена!")
                break
            except Exception:
                continue

        if not downloaded:
            print("[!] Не удалось автоматически скачать базу GeoLite2-Country.mmdb.")
            return

    try:
        GEOIP_READER = maxminddb.open_database(found_path)
        print(f"[+] Оффлайн-база GeoIP подключена: {found_path}")
    except Exception as e:
        print(f"[!] Ошибка открытия базы GeoIP: {e}")

async def resolve_ip(host: str) -> Optional[str]:
    """Быстрый асинхронный DNS-резолв с кэшированием"""
    if not host:
        return None
    # Если это уже чистый IPv4/IPv6
    if re.match(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$', host) or ":" in host:
        return host

    if host in _dns_cache:
        return _dns_cache[host]

    loop = asyncio.get_running_loop()
    try:
        addrinfo = await loop.getaddrinfo(host, None, family=socket.AF_INET)
        if addrinfo:
            ip = addrinfo[0][4][0]
            _dns_cache[host] = ip
            return ip
    except Exception:
        pass
    return None

async def lookup_country(host: str) -> str:
    """Определяет ISO код страны (2 буквы) по хосту или IP"""
    if not GEOIP_READER or not host:
        return "UNKNOWN"

    ip = await resolve_ip(host)
    if not ip:
        return "UNKNOWN"

    try:
        res = GEOIP_READER.get(ip)
        if res and isinstance(res, dict):
            # Стандартный формат MaxMind
            if "country" in res and "iso_code" in res["country"]:
                return res["country"]["iso_code"].upper()
            if "registered_country" in res and "iso_code" in res["registered_country"]:
                return res["registered_country"]["iso_code"].upper()
            # Упрощенные форматы sing-box / loyalsoldier mmdb
            if "country_code" in res:
                return str(res["country_code"]).upper()
    except Exception:
        pass
    return "UNKNOWN"

# --- Сетевые утилиты и Sing-box парсер ---

BANNED_GL_REGIONS = {"RU", "BY", "CN", "HK", "IR", "KP", "CU", "SY"}
_port_counter = 16000
_port_lock = asyncio.Lock()

def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

async def get_next_port_range(count=1):
    global _port_counter
    async with _port_lock:
        start_port = _port_counter
        _port_counter += count
        if _port_counter > 60000:
            _port_counter = 16000
            start_port = 16000
            _port_counter += count
        return start_port

def parse_link_to_singbox(link, tag="proxy"):
    link = link.strip()
    if not link or link.startswith("#"):
        return None

    clean_link = link.split("#")[0]

    try:
        if clean_link.startswith("vless://"):
            u = urllib.parse.urlparse(clean_link)
            host = u.hostname
            port = u.port or 443
            params = urllib.parse.parse_qs(u.query)
            
            outbound = {
                "type": "vless",
                "tag": tag,
                "server": host,
                "server_port": port,
                "uuid": u.username
            }
            security = params.get("security", ["none"])[0]
            if security in ["tls", "reality"]:
                tls_conf = {
                    "enabled": True,
                    "server_name": params.get("sni", [host])[0],
                    "insecure": params.get("insecure", ["0"])[0] == "1" or params.get("allowInsecure", ["0"])[0] == "1",
                    "utls": {"enabled": True, "fingerprint": params.get("fp", ["chrome"])[0]}
                }
                if security == "reality":
                    tls_conf["reality"] = {
                        "enabled": True,
                        "public_key": params.get("pbk", [""])[0],
                        "short_id": params.get("sid", [""])[0]
                    }
                outbound["tls"] = tls_conf
                flow = params.get("flow", [""])[0]
                if flow:
                    outbound["flow"] = flow

            net_type = params.get("type", ["tcp"])[0]
            if net_type == "ws":
                outbound["transport"] = {
                    "type": "ws",
                    "path": params.get("path", ["/"])[0],
                    "headers": {"Host": params.get("host", [params.get("sni", [host])[0]])[0]}
                }
            elif net_type == "grpc":
                outbound["transport"] = {
                    "type": "grpc",
                    "service_name": params.get("serviceName", [""])[0]
                }
            return outbound

        elif clean_link.startswith("vmess://"):
            raw_b64 = clean_link[8:]
            pad = len(raw_b64) % 4
            if pad: raw_b64 += "=" * (4 - pad)
            data = json.loads(base64.b64decode(raw_b64).decode('utf-8'))

            outbound = {
                "type": "vmess",
                "tag": tag,
                "server": data.get("add"),
                "server_port": int(data.get("port")),
                "uuid": data.get("id"),
                "alter_id": int(data.get("aid", 0)),
                "security": data.get("scy", "auto")
            }
            if data.get("tls") == "tls":
                outbound["tls"] = {
                    "enabled": True,
                    "server_name": data.get("sni") or data.get("host") or data.get("add"),
                    "insecure": data.get("allowInsecure") == "1" or data.get("insecure") == "1"
                }
            net_type = data.get("net", "tcp")
            if net_type == "ws":
                outbound["transport"] = {
                    "type": "ws",
                    "path": data.get("path", "/"),
                    "headers": {"Host": data.get("host", "")}
                }
            elif net_type == "grpc":
                outbound["transport"] = {
                    "type": "grpc",
                    "service_name": data.get("path", "")
                }
            return outbound

        elif clean_link.startswith("trojan://"):
            u = urllib.parse.urlparse(clean_link)
            params = urllib.parse.parse_qs(u.query)
            return {
                "type": "trojan",
                "tag": tag,
                "server": u.hostname,
                "server_port": u.port or 443,
                "password": u.username,
                "tls": {
                    "enabled": True,
                    "server_name": params.get("sni", [u.hostname])[0],
                    "insecure": params.get("insecure", ["0"])[0] == "1" or params.get("allowInsecure", ["0"])[0] == "1"
                }
            }

        elif clean_link.startswith("hysteria2://") or clean_link.startswith("hy2://"):
            u = urllib.parse.urlparse(clean_link)
            params = urllib.parse.parse_qs(u.query)
            return {
                "type": "hysteria2",
                "tag": tag,
                "server": u.hostname,
                "server_port": u.port or 443,
                "password": u.username,
                "tls": {
                    "enabled": True,
                    "server_name": params.get("sni", [u.hostname])[0],
                    "insecure": params.get("insecure", ["0"])[0] == "1"
                }
            }

        elif clean_link.startswith("ss://"):
            body = clean_link[5:]
            if "@" in body:
                user_info, host_port = body.split("@", 1)
                if ":" not in user_info:
                    pad = len(user_info) % 4
                    if pad: user_info += "=" * (4 - pad)
                    user_info = base64.b64decode(user_info).decode('utf-8')
                method, password = user_info.split(":", 1)
                host, port = host_port.split(":", 1)
            else:
                pad = len(body) % 4
                if pad: body += "=" * (4 - pad)
                decoded = base64.b64decode(body).decode('utf-8')
                user_info, host_port = decoded.split("@", 1)
                method, password = user_info.split(":", 1)
                host, port = host_port.split(":", 1)

            return {
                "type": "shadowsocks",
                "tag": tag,
                "server": host,
                "server_port": int(port),
                "method": method,
                "password": password
            }
    except Exception:
        return None
    return None

def extract_remark(link):
    remark = "Unknown"
    try:
        if "#" in link:
            remark = urllib.parse.unquote(link.split("#", 1)[1].strip())
        elif link.startswith("vmess://"):
            raw_b64 = link[8:].split("#")[0]
            pad = len(raw_b64) % 4
            if pad: raw_b64 += "=" * (4 - pad)
            data = json.loads(base64.b64decode(raw_b64).decode('utf-8'))
            remark = str(data.get("ps", "VMess"))
    except Exception:
        pass
    
    remark = re.sub(r'[\r\n\t\x00-\x1f\x7f]+', ' ', remark).strip()
    if len(remark) > 150:
        remark = remark[:147] + "..."
    return remark or "Unnamed"

async def wait_for_port(port, proc, timeout=2.5):
    start = asyncio.get_running_loop().time()
    while asyncio.get_running_loop().time() - start < timeout:
        if proc.returncode is not None:
            return False
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=0.08)
            w.close()
            try:
                await w.wait_closed()
            except Exception:
                pass
            return True
        except Exception:
            await asyncio.sleep(0.02)
    return False

async def check_http_via_proxy(proxy_url, check_url=DEFAULT_CHECK_URL, timeout_sec=5.0):
    start_time = asyncio.get_running_loop().time()
    conn_timeout = min(timeout_sec, 2.8)
    timeout = aiohttp.ClientTimeout(total=timeout_sec, connect=conn_timeout)
    connector = aiohttp.TCPConnector(ssl=False)

    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            is_aistudio = "aistudio.google.com" in check_url.lower()

            if is_aistudio:
                try:
                    headers_web = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128.0.0.0 Safari/537.36",
                        "Accept-Language": "en-US,en;q=0.9",
                    }
                    async with session.get("https://aistudio.google.com/", proxy=proxy_url, headers=headers_web, allow_redirects=True) as resp:
                        latency = int((asyncio.get_running_loop().time() - start_time) * 1000)
                        final_url = str(resp.url).lower()
                        if "google.com/sorry" in final_url:
                            return {"ok": False, "status": "blocked", "error": "Бан Google (Sorry/Капча)", "latency": latency}
                        if resp.status == 403:
                            return {"ok": False, "status": "blocked", "error": "Google 403 Forbidden", "latency": latency}
                except asyncio.TimeoutError:
                    return {"ok": False, "status": "dead", "error": "Timeout соединения"}
                except Exception as e:
                    return {"ok": False, "status": "dead", "error": f"Сбой сети: {e}"}

                rpc_url = "https://alkalimakersuite-pa.clients6.google.com/$rpc/google.internal.alkali.applications.makersuite.v1.MakerSuiteService/GetLoggingContext"
                rpc_headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128.0.0.0 Safari/537.36",
                    "Content-Type": "application/json+protobuf",
                    "X-Goog-Api-Client": "gl-js/ boq-makersuite",
                    "Origin": "https://aistudio.google.com",
                    "Referer": "https://aistudio.google.com/",
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9"
                }
                try:
                    async with session.post(rpc_url, proxy=proxy_url, headers=rpc_headers, data="[]") as rpc_resp:
                        rpc_body = await rpc_resp.text()
                        rpc_lower = rpc_body.lower()
                        if "region not supported" in rpc_lower or "failed_precondition" in rpc_lower:
                            return {"ok": False, "status": "blocked", "error": "Region not supported", "latency": latency}
                        if rpc_resp.status == 403:
                            return {"ok": False, "status": "blocked", "error": "MakerSuite 403", "latency": latency}
                except Exception as e:
                    return {"ok": False, "status": "dead", "error": f"Сбой RPC: {e}"}

                try:
                    async with session.get("https://www.google.com/service/check_country/", proxy=proxy_url) as c_resp:
                        if c_resp.status == 200:
                            c_text = await c_resp.text()
                            m = re.search(r'>([A-Z]{2})<', c_text)
                            if m and m.group(1).upper() in BANNED_GL_REGIONS:
                                return {"ok": False, "status": "blocked", "error": f"Регион {m.group(1).upper()}", "latency": latency}
                except Exception:
                    pass

                return {"ok": True, "status": "alive", "latency": latency}

            else:
                async with session.get(check_url, proxy=proxy_url, allow_redirects=True) as resp:
                    latency = int((asyncio.get_running_loop().time() - start_time) * 1000)
                    if resp.status >= 400:
                        return {"ok": False, "status": "dead", "error": f"HTTP {resp.status}", "latency": latency}
                    return {"ok": True, "status": "alive", "latency": latency}

    except asyncio.TimeoutError:
        return {"ok": False, "status": "dead", "error": "Timeout соединения"}
    except Exception as e:
        return {"ok": False, "status": "dead", "error": f"Сбой сети: {e}"}

async def run_with_singbox(raw_link, test_func):
    outbound = parse_link_to_singbox(raw_link)
    if not outbound:
        return {"ok": False, "status": "dead", "error": "Неподдерживаемый протокол"}

    local_port = await get_next_port_range(1)
    cfg = {
        "log": {"level": "panic"},
        "inbounds": [{"type": "mixed", "tag": "mixed-in", "listen": "127.0.0.1", "listen_port": local_port}],
        "outbounds": [outbound]
    }

    fd, cfg_path = tempfile.mkstemp(suffix=".json", prefix="sb_")
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(cfg, f)

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            SING_BOX_BIN, "run", "-c", cfg_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        ready = await wait_for_port(local_port, proc, timeout=2.5)
        if not ready:
            return {"ok": False, "status": "dead", "error": "Ошибка запуска ядра"}

        proxy_url = f"http://127.0.0.1:{local_port}"
        return await test_func(proxy_url)
    finally:
        if proc:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        if os.path.exists(cfg_path):
            try:
                os.remove(cfg_path)
            except Exception:
                pass

async def test_single_config(raw_link, check_url=DEFAULT_CHECK_URL, timeout_sec=5.0):
    async def _test(proxy_url):
        return await check_http_via_proxy(proxy_url, check_url, timeout_sec)
    try:
        return await run_with_singbox(raw_link, _test)
    except Exception as e:
        return {"ok": False, "status": "dead", "error": str(e)}

async def test_batch_configs(batch_items, check_url=DEFAULT_CHECK_URL, timeout_sec=5.0, retry=False):
    valid_items = []
    results = {}

    for item in batch_items:
        outbound = parse_link_to_singbox(item["raw"], tag=f"out_{item['id']}")
        if not outbound:
            results[item["id"]] = {"ok": False, "status": "dead", "error": "Неподдерживаемый протокол"}
        else:
            valid_items.append((item, outbound))

    if not valid_items:
        return [results[item["id"]] for item in batch_items]

    count = len(valid_items)
    port_base = await get_next_port_range(count)

    inbounds = []
    outbounds = []
    rules = []
    item_port_map = []

    for idx, (item, outbound) in enumerate(valid_items):
        port = port_base + idx
        in_tag = f"in_{item['id']}"
        inbounds.append({
            "type": "mixed",
            "tag": in_tag,
            "listen": "127.0.0.1",
            "listen_port": port
        })
        outbounds.append(outbound)
        rules.append({
            "inbound": [in_tag],
            "outbound": outbound["tag"]
        })
        item_port_map.append((item, port))

    cfg = {
        "log": {"level": "panic"},
        "inbounds": inbounds,
        "outbounds": outbounds,
        "route": {"rules": rules}
    }

    fd, cfg_path = tempfile.mkstemp(suffix=".json", prefix="sb_batch_")
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(cfg, f)

    proc = None
    batch_started = False
    try:
        proc = await asyncio.create_subprocess_exec(
            SING_BOX_BIN, "run", "-c", cfg_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        batch_started = await wait_for_port(port_base, proc, timeout=3.0)

        if batch_started:
            async def do_single_ping(it, port):
                proxy_url = f"http://127.0.0.1:{port}"
                res = await check_http_via_proxy(proxy_url, check_url, timeout_sec)
                if not res.get("ok") and retry and res.get("error") == "Timeout соединения":
                    res = await check_http_via_proxy(proxy_url, check_url, timeout_sec)
                return it["id"], res

            tasks = [do_single_ping(it, port) for it, port in item_port_map]
            batch_res = await asyncio.gather(*tasks)
            for it_id, res in batch_res:
                results[it_id] = res

    finally:
        if proc:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        if os.path.exists(cfg_path):
            try:
                os.remove(cfg_path)
            except Exception:
                pass

    if not batch_started:
        for it, _ in valid_items:
            res = await test_single_config(it["raw"], check_url, timeout_sec)
            results[it["id"]] = res

    return [results.get(item["id"], {"ok": False, "status": "dead", "error": "Сбой теста"}) for item in batch_items]

async def test_speed_config(raw_link, timeout_sec=8):
    async def _speed_test(proxy_url):
        timeout = aiohttp.ClientTimeout(total=timeout_sec, connect=3.0)
        connector = aiohttp.TCPConnector(ssl=False)
        total_bytes = 0
        start_time = asyncio.get_running_loop().time()

        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.get(SPEEDTEST_URL, proxy=proxy_url) as resp:
                if resp.status != 200:
                    return {"ok": False, "error": f"HTTP {resp.status}"}
                
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    total_bytes += len(chunk)
                    if asyncio.get_running_loop().time() - start_time >= timeout_sec:
                        break

        duration = asyncio.get_running_loop().time() - start_time
        if duration > 0 and total_bytes > 0:
            speed_mbps = round((total_bytes * 8) / (duration * 1_000_000), 2)
            return {"ok": True, "speed": speed_mbps, "bytes": total_bytes}
        return {"ok": False, "error": "Нулевой поток"}

    try:
        return await run_with_singbox(raw_link, _speed_test)
    except Exception as e:
        return {"ok": False, "error": str(e) or "Timeout"}

async def fetch_source_text(url):
    urls_to_try = [url]
    if "raw.githubusercontent.com" in url:
        urls_to_try.append(url.replace("raw.githubusercontent.com", "raw.gitmirror.com"))
        urls_to_try.append(f"https://ghproxy.net/{url}")

    timeout = aiohttp.ClientTimeout(total=8)
    for target in urls_to_try:
        try:
            async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "v2rayN/6.0"}) as session:
                async with session.get(target) as resp:
                    if resp.status == 200:
                        return await resp.text()
        except Exception:
            continue
    raise Exception("Не удалось скачать подписку. Вставьте ключи текстом.")

# --- API Обработчики ---

async def handle_parse(request):
    try:
        data = await request.json()
        source = data.get("source", "").strip()
        
        if source.startswith("http://") or source.startswith("https://"):
            source = await fetch_source_text(source)

        if "://" not in source:
            try:
                clean_src = re.sub(r'\s+', '', source)
                pad = len(clean_src) % 4
                if pad: clean_src += "=" * (4 - pad)
                source = base64.b64decode(clean_src).decode('utf-8', errors='ignore')
            except Exception:
                pass

        lines = source.splitlines()
        parsed_entries = []
        for idx, line in enumerate(lines):
            line = line.strip()
            if not line: continue
            outbound = parse_link_to_singbox(line)
            if outbound:
                # Получаем каноническую сигнатуру узла для дедупликации (без тега)
                canonical_cfg = outbound.copy()
                canonical_cfg.pop("tag", None)
                signature = json.dumps(canonical_cfg, sort_keys=True)

                parsed_entries.append({
                    "id": idx + 1,
                    "raw": line,
                    "type": outbound["type"].upper(),
                    "remark": extract_remark(line),
                    "host": outbound.get("server", "unknown"),
                    "signature": signature
                })

        # Асинхронно резолвим страны для всех найденных узлов
        async def resolve_item_country(it):
            c = await lookup_country(it["host"])
            it["country"] = c
            return it

        items = await asyncio.gather(*[resolve_item_country(it) for it in parsed_entries])
        return web.json_response({"success": True, "items": items})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)

async def handle_ping_batch(request):
    try:
        data = await request.json()
        items = data.get("items", [])
        timeout = int(data.get("timeout", 4500)) / 1000.0
        check_url = data.get("check_url", "").strip() or DEFAULT_CHECK_URL
        retry = bool(data.get("retry", False))

        results = await test_batch_configs(items, check_url=check_url, timeout_sec=timeout, retry=retry)
        return web.json_response({"ok": True, "results": results})
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)

async def handle_ping(request):
    try:
        data = await request.json()
        raw_link = data["raw"]
        timeout = int(data.get("timeout", 4500)) / 1000.0
        check_url = data.get("check_url", "").strip() or DEFAULT_CHECK_URL
        retry = bool(data.get("retry", False))

        res = await test_single_config(raw_link, check_url, timeout)
        if not res.get("ok") and retry and res.get("error") == "Timeout соединения":
            res = await test_single_config(raw_link, check_url, timeout)

        return web.json_response(res)
    except Exception as e:
        return web.json_response({"ok": False, "status": "dead", "error": str(e)})

async def handle_speed(request):
    try:
        data = await request.json()
        raw_link = data["raw"]
        timeout = int(data.get("timeout", 8000)) / 1000.0
        res = await test_speed_config(raw_link, timeout_sec=timeout)
        return web.json_response(res)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)})

async def handle_share(request):
    try:
        data = await request.json()
        raw_keys = data.get("keys", [])
        if not raw_keys:
            return web.json_response({"ok": False, "error": "Список ключей пуст"})

        request.app['alive_keys_cache'] = raw_keys
        lan_ip = get_lan_ip()
        local_sub_url = f"http://{lan_ip}:{PORT}/sub"

        cloud_url = None
        try:
            content_text = "\n".join(raw_keys)
            timeout = aiohttp.ClientTimeout(total=5)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                payload = {
                    "content": content_text,
                    "syntax": "text",
                    "expiry_days": "1"
                }
                async with session.post("https://dpaste.org/api/", data=payload) as resp:
                    if resp.status in (200, 201):
                        resp_url = (await resp.text()).strip().replace('"', '')
                        if resp_url.startswith("http"):
                            cloud_url = f"{resp_url.rstrip('/')}/raw"
        except Exception:
            pass

        return web.json_response({
            "ok": True,
            "local_url": local_sub_url,
            "cloud_url": cloud_url,
            "count": len(raw_keys)
        })
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=500)

async def handle_sub(request):
    keys = request.app.get('alive_keys_cache', [])
    if not keys:
        return web.Response(text="No active keys found. Run test first.", status=404)

    text_data = "\n".join(keys)
    if request.query.get("b64") == "1":
        text_data = base64.b64encode(text_data.encode("utf-8")).decode("utf-8")

    return web.Response(
        text=text_data,
        content_type="text/plain",
        headers={
            "Content-Disposition": 'inline; filename="subscription.txt"',
            "Profile-Update-Interval": "1"
        }
    )

# --- Встроенный Web UI ---

HTML_CONTENT = r"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>CoreProxy Lab — Быстрая диагностика узлов</title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
  <style>
    :root {
      --bg: #0b0f17;
      --surface: #121824;
      --surface-hover: #172033;
      --card: #141c2c;
      --border: #1f2a3f;
      --border-light: #2c3a57;
      
      --accent: #2563eb;
      --accent-hover: #1d4ed8;
      --accent-light: rgba(37, 99, 235, 0.15);
      
      --success: #10b981;
      --success-bg: rgba(16, 185, 129, 0.12);
      --danger: #f43f5e;
      --danger-bg: rgba(244, 63, 94, 0.12);
      --warning: #f59e0b;
      --warning-bg: rgba(245, 158, 11, 0.12);
      --speed: #8b5cf6;
      --speed-bg: rgba(139, 92, 246, 0.14);
      
      --text: #f1f5f9;
      --text-muted: #94a3b8;
      --text-dim: #64748b;
      --radius: 8px;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; min-width: 0; }
    
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding: 24px 16px;
    }

    .container {
      max-width: 1440px;
      margin: 0 auto;
      display: flex;
      flex-direction: column;
      gap: 18px;
    }

    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 4px 6px;
    }
    .header h1 {
      font-size: 1.35rem;
      font-weight: 700;
      letter-spacing: -0.02em;
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .header-badge {
      font-size: 0.72rem;
      background: #1e293b;
      color: #38bdf8;
      padding: 3px 8px;
      border-radius: 9999px;
      border: 1px solid #334155;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }

    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 18px;
    }

    .textarea-wrapper { position: relative; }
    textarea {
      width: 100%;
      height: 95px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      color: var(--text);
      padding: 10px 14px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.85rem;
      resize: vertical;
      outline: none;
      transition: border-color 0.15s ease;
    }
    textarea:focus, input:focus, select:focus { border-color: var(--accent); }

    .control-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 12px;
      margin-top: 14px;
    }
    .field {
      display: flex;
      flex-direction: column;
      gap: 5px;
    }
    .field label {
      font-size: 0.75rem;
      color: var(--text-muted);
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    input[type="text"], input[type="number"], select {
      background: var(--surface);
      border: 1px solid var(--border);
      color: var(--text);
      border-radius: 6px;
      padding: 7px 11px;
      font-size: 0.86rem;
      outline: none;
      width: 100%;
    }
    .checkbox-field {
      display: flex;
      align-items: center;
      gap: 8px;
      cursor: pointer;
      font-size: 0.84rem;
      user-select: none;
      height: 100%;
      padding-top: 20px;
    }

    .action-bar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-top: 16px;
      padding-top: 14px;
      border-top: 1px solid var(--border);
    }
    .action-group {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }

    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 7px 14px;
      font-size: 0.84rem;
      font-weight: 600;
      border-radius: 6px;
      cursor: pointer;
      border: 1px solid var(--border-light);
      background: var(--surface);
      color: var(--text);
      transition: all 0.15s ease;
      white-space: nowrap;
      user-select: none;
    }
    .btn:hover:not(:disabled) {
      background: var(--surface-hover);
      border-color: #3b4d71;
    }
    .btn:active:not(:disabled) { transform: translateY(1px); }
    .btn:disabled { opacity: 0.4; cursor: not-allowed; }

    .btn-primary { background: var(--accent); border-color: var(--accent); color: #fff; }
    .btn-primary:hover:not(:disabled) { background: var(--accent-hover); border-color: var(--accent-hover); }

    .btn-success { background: #059669; border-color: #059669; color: #fff; }
    .btn-success:hover:not(:disabled) { background: #047857; border-color: #047857; }

    .btn-speed { background: #7c3aed; border-color: #7c3aed; color: #fff; }
    .btn-speed:hover:not(:disabled) { background: #6d28d9; border-color: #6d28d9; }

    .btn-danger { background: #dc2626; border-color: #dc2626; color: #fff; }
    .btn-danger:hover:not(:disabled) { background: #b91c1c; border-color: #b91c1c; }

    .btn-warning { background: #d97706; border-color: #d97706; color: #fff; }
    .btn-warning:hover:not(:disabled) { background: #b45309; border-color: #b45309; }

    .btn-mobile { background: #0284c7; border-color: #0284c7; color: #fff; }
    .btn-mobile:hover:not(:disabled) { background: #0369a1; border-color: #0369a1; }

    .stats-bar {
      display: grid;
      grid-template-columns: repeat(5, 1fr);
      gap: 12px;
    }
    @media (max-width: 960px) { .stats-bar { grid-template-columns: repeat(2, 1fr); } }

    .stat-card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 12px 16px;
      display: flex;
      flex-direction: column;
      gap: 2px;
    }
    .stat-label {
      font-size: 0.72rem;
      font-weight: 600;
      color: var(--text-dim);
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .stat-value {
      font-size: 1.35rem;
      font-weight: 700;
      letter-spacing: -0.02em;
    }
    .stat-value.alive { color: var(--success); }
    .stat-value.dead { color: var(--danger); }
    .stat-value.timer { color: #38bdf8; font-variant-numeric: tabular-nums; }

    .list-toolbar {
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      margin-bottom: 14px;
    }
    .filter-group {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
    }

    .grid-view {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
    }
    @media (max-width: 960px) { .grid-view { grid-template-columns: 1fr; } }

    .column-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 8px;
      font-size: 0.9rem;
      font-weight: 600;
      color: var(--text-muted);
    }
    .column-count {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 9999px;
      padding: 2px 8px;
      font-size: 0.75rem;
      color: var(--text);
    }

    .list-container {
      height: 580px;
      overflow-y: auto;
      background: #090d14;
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 8px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 0.82rem;
    }
    .list-container::-webkit-scrollbar { width: 6px; }
    .list-container::-webkit-scrollbar-thumb { background: var(--border-light); border-radius: 3px; }

    details.group-box {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 6px;
      margin-bottom: 8px;
      overflow: hidden;
    }
    details.group-box summary {
      cursor: pointer;
      padding: 9px 12px;
      font-weight: 600;
      background: rgba(255, 255, 255, 0.02);
      display: flex;
      justify-content: space-between;
      align-items: center;
      user-select: none;
      font-size: 0.8rem;
    }
    details.group-box summary:hover { background: var(--surface-hover); }
    .group-content { padding: 4px; }
    .group-tag { font-size: 0.75rem; color: var(--text-dim); }

    .item-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 6px 10px;
      border-radius: 4px;
      margin-bottom: 2px;
      gap: 10px;
    }
    .item-row:hover { background: rgba(255, 255, 255, 0.04); }
    .item-title {
      flex: 1;
      min-width: 0;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      font-size: 0.8rem;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .item-status {
      flex-shrink: 0;
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .badge {
      display: inline-flex;
      align-items: center;
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 0.72rem;
      font-weight: 600;
      white-space: nowrap;
    }
    .badge-proto {
      background: #1e293b;
      color: #94a3b8;
      border: 1px solid #334155;
      font-size: 0.68rem;
    }
    .badge-proto.vless { color: #38bdf8; }
    .badge-proto.vmess { color: #fbbf24; }
    .badge-proto.hysteria2 { color: #c084fc; }
    .badge-proto.trojan { color: #f472b6; }
    .badge-proto.shadowsocks { color: #4ade80; }

    .badge-country {
      background: #182235;
      color: #38bdf8;
      border: 1px solid #283858;
      font-size: 0.68rem;
      letter-spacing: 0.03em;
    }

    .badge-queued { background: #1e293b; color: #64748b; }
    .badge-checking { background: var(--warning-bg); color: var(--warning); border: 1px solid rgba(245, 158, 11, 0.3); }
    .badge-alive { background: var(--success-bg); color: var(--success); border: 1px solid rgba(16, 185, 129, 0.3); }
    .badge-dead { background: var(--danger-bg); color: var(--danger); border: 1px solid rgba(244, 63, 94, 0.3); }
    .badge-speed { background: var(--speed-bg); color: #c4b5fd; border: 1px solid rgba(139, 92, 246, 0.3); }

    .empty-state {
      padding: 40px 16px;
      text-align: center;
      color: var(--text-dim);
      font-size: 0.84rem;
    }

    /* Модальное окно передачи на смартфон */
    .modal-overlay {
      position: fixed;
      top: 0; left: 0; right: 0; bottom: 0;
      background: rgba(0, 0, 0, 0.75);
      display: none;
      align-items: center;
      justify-content: center;
      z-index: 1000;
      backdrop-filter: blur(4px);
    }
    .modal-overlay.active { display: flex; }
    .modal-card {
      background: var(--card);
      border: 1px solid var(--border-light);
      border-radius: 12px;
      padding: 24px;
      max-width: 480px;
      width: 90%;
      text-align: center;
      display: flex;
      flex-direction: column;
      gap: 16px;
      box-shadow: 0 16px 36px rgba(0,0,0,0.5);
    }
    .modal-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .modal-header h3 { font-size: 1.1rem; }
    .modal-close {
      background: transparent;
      border: none;
      color: var(--text-muted);
      font-size: 1.3rem;
      cursor: pointer;
    }
    .qr-box {
      background: #ffffff;
      padding: 16px;
      border-radius: 8px;
      width: 220px;
      height: 220px;
      margin: 0 auto;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .link-box {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 8px 12px;
      font-family: monospace;
      font-size: 0.8rem;
      word-break: break-all;
      user-select: all;
    }
    .tab-group {
      display: flex;
      gap: 6px;
      background: var(--surface);
      padding: 4px;
      border-radius: 8px;
    }
    .tab-btn {
      flex: 1;
      padding: 6px;
      font-size: 0.78rem;
      border: none;
      background: transparent;
      color: var(--text-muted);
      border-radius: 6px;
      cursor: pointer;
      font-weight: 600;
    }
    .tab-btn.active { background: var(--accent); color: #fff; }
  </style>
</head>
<body>

<div class="container">
  
  <header class="header">
    <h1>
      CoreProxy Lab
      <span class="header-badge">sing-box & GeoIP Lab</span>
    </h1>
    <button class="btn btn-danger" id="btnClearSession" title="Очистить память сессии в браузере">🗑 Сброс сессии</button>
  </header>

  <section class="card">
    <div class="textarea-wrapper">
      <textarea id="inputSource" placeholder="Вставьте ссылку на подписку (http/https/base64) или список ключей (vless://, hysteria2://, vmess://, ss://, trojan://)..."></textarea>
    </div>

    <div class="control-grid">
      <div class="field" style="grid-column: span 2;">
        <label>Целевой URL для проверки</label>
        <input type="text" id="checkUrl" value="https://cp.cloudflare.com/generate_204">
      </div>

      <div class="field">
        <label>Готовые пресеты</label>
        <select id="presetSelect" onchange="document.getElementById('checkUrl').value = this.value; saveSettingsToStorage();">
          <option value="https://cp.cloudflare.com/generate_204">Cloudflare 204 (Быстрый)</option>
          <option value="http://www.google.com/generate_204">Google 204 (HTTP)</option>
          <option value="https://www.youtube.com/generate_204">YouTube (204)</option>
          <option value="https://web.telegram.org">Telegram Web</option>
          <option value="https://github.com">GitHub</option>
          <option value="https://aistudio.google.com">Google AI Studio</option>
        </select>
      </div>

      <div class="field">
        <label>Параллельных ядер</label>
        <input type="number" id="concurrency" value="2" min="1" max="5">
      </div>

      <div class="field">
        <label>Узлов в пачке</label>
        <input type="number" id="batchSize" value="25" min="5" max="50">
      </div>

      <div class="field">
        <label>Таймаут (мс)</label>
        <input type="number" id="timeout" value="4500" step="500">
      </div>

      <label class="checkbox-field">
        <input type="checkbox" id="retryFailed" checked>
        <span>Повтор при таймауте (1 retry)</span>
      </label>
    </div>

    <div class="action-bar">
      <div class="action-group">
        <button class="btn btn-primary" id="btnLoad">📥 Загрузить и Распарсить</button>
        <button class="btn btn-warning" id="btnDeduplicate" title="Удалить дубликаты с одинаковыми параметрами подключения">✂ Удалить дубликаты</button>
        <button class="btn btn-success" id="btnStart" disabled>▶ Запустить проверку</button>
        <button class="btn btn-danger" id="btnStop" disabled>⏹ Стоп</button>
      </div>

      <div class="action-group">
        <button class="btn" id="btnToggleAll">Свернуть/Развернуть</button>
        <button class="btn" id="btnSelectAllGroups">Выбрать все</button>
        <button class="btn" id="btnUnselectAllGroups">Снять выбор</button>
      </div>
    </div>
  </section>

  <!-- Карточки статистики -->
  <section class="stats-bar">
    <div class="stat-card">
      <span class="stat-label">Всего ключей</span>
      <span class="stat-value" id="statTotal">0</span>
    </div>
    <div class="stat-card">
      <span class="stat-label">Проверено</span>
      <span class="stat-value" id="statChecked">0</span>
    </div>
    <div class="stat-card">
      <span class="stat-label">Рабочие (Alive)</span>
      <span class="stat-value alive" id="statAlive">0</span>
    </div>
    <div class="stat-card">
      <span class="stat-label">Нерабочие (Dead)</span>
      <span class="stat-value dead" id="statDead">0</span>
    </div>
    <div class="stat-card">
      <span class="stat-label">Время теста</span>
      <span class="stat-value timer" id="statTime">00:00</span>
    </div>
  </section>

  <section class="card">
    <div class="list-toolbar">
      <div class="filter-group">
        <input type="text" id="filterInput" placeholder="🔍 Поиск по имени, хосту..." style="width: 220px;">
        <select id="countryFilter" style="width: 140px;">
          <option value="ALL">🌍 Все страны</option>
        </select>
        <select id="sortSelect" style="width: 170px;">
          <option value="ping_asc">Сортировка: Пинг ↑</option>
          <option value="speed_desc">Сортировка: Скорость ↓</option>
          <option value="country_asc">Сортировка: Страна A-Z</option>
          <option value="proto_asc">Сортировка: Протокол</option>
        </select>
      </div>
      
      <div class="action-group">
        <button class="btn btn-mobile" id="btnShareMobile" disabled>📱 QR на телефон</button>
        <button class="btn btn-speed" id="btnSpeedTest" disabled>⚡ Замерить скорость</button>
        <button class="btn btn-success" id="btnCopyAlive">📋 Скопировать</button>
        <button class="btn btn-success" id="btnExportAlive">💾 Скачать .txt</button>
        <button class="btn btn-danger" id="btnClearDead">Удалить мертвые</button>
      </div>
    </div>

    <div class="grid-view">
      <div>
        <div class="column-header">
          <span>Все узлы по группам</span>
          <span class="column-count" id="allCount">0</span>
        </div>
        <div class="list-container" id="allList">
          <div class="empty-state">Список узлов пуст. Вставьте подписку и нажмите «Загрузить».</div>
        </div>
      </div>

      <div>
        <div class="column-header">
          <span style="color: var(--success);">Успешно прошедшие тест (Alive)</span>
          <span class="column-count" id="workingCount">0</span>
        </div>
        <div class="list-container" id="workingList">
          <div class="empty-state">Пока нет подтвержденных рабочих узлов.</div>
        </div>
      </div>
    </div>
  </section>

</div>

<!-- Модальное окно QR -->
<div class="modal-overlay" id="qrModal">
  <div class="modal-card">
    <div class="modal-header">
      <h3 id="modalTitle">📲 Импорт на смартфон</h3>
      <button class="modal-close" id="modalClose">&times;</button>
    </div>

    <div class="tab-group">
      <button class="tab-btn active" id="tabWifi">🏠 Домашний Wi-Fi (Локально)</button>
      <button class="tab-btn" id="tabCloud">☁️ Любой интернет (dpaste)</button>
    </div>

    <div class="qr-box" id="qrcode"></div>
    <div class="link-box" id="qrLinkText">Загрузка ссылки...</div>
    <p style="font-size: 0.76rem; color: var(--text-dim);">
      Отсканируйте камерой смартфона или добавьте ссылку как подписку в <b>v2rayNG</b>, <b>Sing-box</b> или <b>Streisand</b>.
    </p>
  </div>
</div>

<script>
  const CHUNK_SIZE = 100;
  let items = [];
  let isRunning = false;
  let isSpeedTesting = false;
  let needStatsUpdate = false;
  let allSpoilersOpen = false;
  let selectedGroups = new Set();

  let testStartTime = null;
  let timerInterval = null;

  let localShareUrl = "";
  let cloudShareUrl = "";

  const btnLoad = document.getElementById('btnLoad');
  const btnDeduplicate = document.getElementById('btnDeduplicate');
  const btnStart = document.getElementById('btnStart');
  const btnStop = document.getElementById('btnStop');
  const btnSpeedTest = document.getElementById('btnSpeedTest');
  const btnShareMobile = document.getElementById('btnShareMobile');
  const btnToggleAll = document.getElementById('btnToggleAll');
  const btnSelectAllGroups = document.getElementById('btnSelectAllGroups');
  const btnUnselectAllGroups = document.getElementById('btnUnselectAllGroups');
  const btnCopyAlive = document.getElementById('btnCopyAlive');
  const btnExportAlive = document.getElementById('btnExportAlive');
  const btnClearDead = document.getElementById('btnClearDead');
  const btnClearSession = document.getElementById('btnClearSession');
  const filterInput = document.getElementById('filterInput');
  const countryFilter = document.getElementById('countryFilter');
  const sortSelect = document.getElementById('sortSelect');

  // Флаги стран в эмодзи (по ISO коду)
  function getCountryFlag(iso) {
    if (!iso || iso === 'UNKNOWN') return '🌐';
    return iso.toUpperCase().replace(/./g, char => 
      String.fromCodePoint(char.charCodeAt(0) + 127397)
    );
  }

  // --- Сохранение сессии в localStorage ---

  function saveSession() {
    try {
      localStorage.setItem('coreproxy_items', JSON.stringify(items));
      saveSettingsToStorage();
    } catch (e) {
      console.warn('Не удалось сохранить сессию:', e);
    }
  }

  function saveSettingsToStorage() {
    const settings = {
      checkUrl: document.getElementById('checkUrl').value,
      concurrency: document.getElementById('concurrency').value,
      batchSize: document.getElementById('batchSize').value,
      timeout: document.getElementById('timeout').value,
      retryFailed: document.getElementById('retryFailed').checked,
      sort: sortSelect.value
    };
    localStorage.setItem('coreproxy_settings', JSON.stringify(settings));
  }

  function loadSession() {
    try {
      const savedSettings = localStorage.getItem('coreproxy_settings');
      if (savedSettings) {
        const s = JSON.parse(savedSettings);
        if (s.checkUrl) document.getElementById('checkUrl').value = s.checkUrl;
        if (s.concurrency) document.getElementById('concurrency').value = s.concurrency;
        if (s.batchSize) document.getElementById('batchSize').value = s.batchSize;
        if (s.timeout) document.getElementById('timeout').value = s.timeout;
        if (typeof s.retryFailed === 'boolean') document.getElementById('retryFailed').checked = s.retryFailed;
        if (s.sort) sortSelect.value = s.sort;
      }

      const savedItems = localStorage.getItem('coreproxy_items');
      if (savedItems) {
        items = JSON.parse(savedItems);
        if (Array.isArray(items) && items.length > 0) {
          const totalGroups = Math.ceil(items.length / CHUNK_SIZE);
          selectedGroups = new Set(Array.from({ length: totalGroups }, (_, i) => i));
          updateCountryFilterList();
          updateStats();
          renderAllGroups();
          renderWorkingList();
          btnStart.disabled = false;
          const aliveCount = items.filter(i => i.status === 'alive').length;
          btnSpeedTest.disabled = (aliveCount === 0);
          btnShareMobile.disabled = (aliveCount === 0);
        }
      }
    } catch (e) {
      console.warn('Ошибка загрузки сессии:', e);
    }
  }

  btnClearSession.onclick = () => {
    if (!confirm('Вы действительно хотите очистить текущую сессию и список ключей?')) return;
    localStorage.removeItem('coreproxy_items');
    items = [];
    selectedGroups.clear();
    document.getElementById('inputSource').value = '';
    document.getElementById('allList').innerHTML = '<div class="empty-state">Список узлов пуст. Вставьте подписку и нажмите «Загрузить».</div>';
    document.getElementById('workingList').innerHTML = '<div class="empty-state">Пока нет подтвержденных рабочих узлов.</div>';
    document.getElementById('allCount').innerText = '0';
    document.getElementById('workingCount').innerText = '0';
    document.getElementById('statTime').innerText = '00:00';
    btnStart.disabled = true;
    btnSpeedTest.disabled = true;
    btnShareMobile.disabled = true;
    updateCountryFilterList();
    updateStats();
  };

  // --- Управление таймером ---

  function startTimer() {
    testStartTime = performance.now();
    clearInterval(timerInterval);
    const statTimeEl = document.getElementById('statTime');
    timerInterval = setInterval(() => {
      const elapsed = (performance.now() - testStartTime) / 1000;
      const mins = Math.floor(elapsed / 60).toString().padStart(2, '0');
      const secs = Math.floor(elapsed % 60).toString().padStart(2, '0');
      statTimeEl.innerText = `${mins}:${secs}`;
    }, 200);
  }

  function stopTimer(completedCount) {
    clearInterval(timerInterval);
    if (!testStartTime) return;
    const durationSec = Math.max((performance.now() - testStartTime) / 1000, 0.1);
    const statTimeEl = document.getElementById('statTime');
    
    if (completedCount > 0) {
      const perSec = (completedCount / durationSec).toFixed(1);
      statTimeEl.innerText = `${durationSec.toFixed(1)}с (${perSec}/с)`;
      statTimeEl.title = `Всего: ${durationSec.toFixed(2)} сек, скорость: ${perSec} ключей в секунду`;
    } else {
      statTimeEl.innerText = `${durationSec.toFixed(1)}с`;
    }
  }

  window.toggleGroupSelection = function(gIndex, isChecked) {
    if (isChecked) selectedGroups.add(gIndex);
    else selectedGroups.delete(gIndex);
  };

  btnSelectAllGroups.onclick = () => {
    const totalGroups = Math.ceil(items.length / CHUNK_SIZE);
    selectedGroups = new Set(Array.from({ length: totalGroups }, (_, i) => i));
    document.querySelectorAll('.group-select-chk').forEach(chk => chk.checked = true);
  };

  btnUnselectAllGroups.onclick = () => {
    selectedGroups.clear();
    document.querySelectorAll('.group-select-chk').forEach(chk => chk.checked = false);
  };

  function escapeHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function updateCountryFilterList() {
    const countries = new Map();
    items.forEach(it => {
      const c = it.country || 'UNKNOWN';
      countries.set(c, (countries.get(c) || 0) + 1);
    });

    const currentVal = countryFilter.value;
    countryFilter.innerHTML = '<option value="ALL">🌍 Все страны</option>';
    
    const sorted = [...countries.entries()].sort((a, b) => b[1] - a[1]);
    sorted.forEach(([c, count]) => {
      const opt = document.createElement('option');
      opt.value = c;
      opt.innerText = `${getCountryFlag(c)} ${c} (${count})`;
      countryFilter.appendChild(opt);
    });

    if (countries.has(currentVal)) {
      countryFilter.value = currentVal;
    } else {
      countryFilter.value = 'ALL';
    }
  }

  // --- Загрузка и удаление дубликатов ---

  btnLoad.onclick = async () => {
    const source = document.getElementById('inputSource').value.trim();
    if (!source) return alert('Введите ссылку или ключи!');
    
    btnLoad.disabled = true;
    btnLoad.innerText = 'Загрузка и GeoIP...';
    try {
      const res = await fetch('/api/parse', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source })
      });
      const data = await res.json();
      if (!data.success) throw new Error(data.error);

      items = data.items.map((it, idx) => ({
        ...it,
        group: Math.floor(idx / CHUNK_SIZE),
        status: 'idle',
        latency: null,
        speed: null,
        error: null,
        country: it.country || 'UNKNOWN'
      }));
      
      const totalGroups = Math.ceil(items.length / CHUNK_SIZE);
      selectedGroups = new Set(Array.from({ length: totalGroups }, (_, i) => i));

      document.getElementById('statTime').innerText = '00:00';
      updateCountryFilterList();
      updateStats();
      renderAllGroups();
      document.getElementById('workingList').innerHTML = '<div class="empty-state">Пока нет подтвержденных рабочих узлов.</div>';
      document.getElementById('workingCount').innerText = '0';
      btnStart.disabled = items.length === 0;
      btnSpeedTest.disabled = true;
      btnShareMobile.disabled = true;
      saveSession();

      if (items.length === 0) alert('Ключи не найдены.');
    } catch (err) {
      alert('Ошибка при загрузке: ' + err.message);
    } finally {
      btnLoad.disabled = false;
      btnLoad.innerText = '📥 Загрузить и Распарсить';
    }
  };

  btnDeduplicate.onclick = () => {
    if (items.length === 0) return alert('Список узлов пуст!');
    const beforeCount = items.length;
    const seenSignatures = new Set();
    const uniqueItems = [];

    items.forEach(it => {
      const sig = it.signature || it.raw.split('#')[0];
      if (!seenSignatures.has(sig)) {
        seenSignatures.add(sig);
        uniqueItems.push(it);
      }
    });

    const removed = beforeCount - uniqueItems.length;
    if (removed === 0) {
      return alert('Дубликатов не обнаружено. Все конфигурации уникальны!');
    }

    // Переиндексируем группы
    items = uniqueItems.map((it, idx) => ({
      ...it,
      id: idx + 1,
      group: Math.floor(idx / CHUNK_SIZE)
    }));

    const totalGroups = Math.ceil(items.length / CHUNK_SIZE);
    selectedGroups = new Set(Array.from({ length: totalGroups }, (_, i) => i));

    updateCountryFilterList();
    updateStats();
    renderAllGroups();
    renderWorkingList();
    saveSession();
    alert(`Удалено дубликатов: ${removed} шт.\nОсталось уникальных узлов: ${items.length}`);
  };

  // --- Запуск проверок ---

  btnStart.onclick = async () => {
    let queue = items.filter(it => selectedGroups.has(it.group));
    if (queue.length === 0) return alert('Выберите хотя бы одну группу для проверки!');

    isRunning = true;
    btnStart.disabled = true;
    btnStop.disabled = false;
    btnSpeedTest.disabled = true;
    btnShareMobile.disabled = true;

    queue.forEach(item => {
      item.status = 'idle';
      item.latency = null;
      item.speed = null;
      item.error = null;
      renderItemStatus(item);
    });

    const concurrency = Math.min(Math.max(parseInt(document.getElementById('concurrency').value, 10) || 2, 1), 6);
    const batchSize = Math.min(Math.max(parseInt(document.getElementById('batchSize').value, 10) || 25, 5), 50);
    const timeout = parseInt(document.getElementById('timeout').value, 10) || 4500;
    const checkUrl = document.getElementById('checkUrl').value.trim() || 'https://cp.cloudflare.com/generate_204';
    const retry = document.getElementById('retryFailed').checked;

    let chunks = [];
    for (let i = 0; i < queue.length; i += batchSize) {
      chunks.push(queue.slice(i, i + batchSize));
    }

    startTimer();

    const statsInterval = setInterval(() => {
      if (needStatsUpdate) {
        updateStats();
        renderWorkingList();
        saveSession();
        needStatsUpdate = false;
      }
    }, 250);

    const runBatchWorker = async () => {
      while (chunks.length > 0 && isRunning) {
        const chunk = chunks.shift();
        chunk.forEach(it => {
          it.status = 'checking';
          renderItemStatus(it);
        });

        try {
          const res = await fetch('/api/ping_batch', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              items: chunk.map(it => ({ id: it.id, raw: it.raw })),
              timeout,
              check_url: checkUrl,
              retry
            })
          });
          const data = await res.json();
          if (data.ok && Array.isArray(data.results)) {
            const resMap = new Map(data.results.map((r, i) => [chunk[i].id, r]));
            chunk.forEach(item => {
              const r = resMap.get(item.id);
              if (r && r.ok) {
                item.status = 'alive';
                item.latency = r.latency;
              } else {
                item.status = 'dead';
                item.error = (r && r.error) ? r.error : 'Dead';
              }
              renderItemStatus(item);
            });
          } else {
            chunk.forEach(item => {
              item.status = 'dead';
              item.error = 'Сбой пачки';
              renderItemStatus(item);
            });
          }
        } catch (e) {
          chunk.forEach(item => {
            item.status = 'dead';
            item.error = 'Сбой сети';
            renderItemStatus(item);
          });
        }
        needStatsUpdate = true;
      }
    };

    const workers = [];
    for (let i = 0; i < concurrency; i++) workers.push(runBatchWorker());

    await Promise.all(workers);
    clearInterval(statsInterval);
    updateStats();
    renderWorkingList();
    saveSession();
    isRunning = false;
    btnStart.disabled = false;
    btnStop.disabled = true;

    stopTimer(queue.length);

    const aliveCount = items.filter(i => i.status === 'alive').length;
    btnSpeedTest.disabled = (aliveCount === 0);
    btnShareMobile.disabled = (aliveCount === 0);
  };

  btnStop.onclick = () => {
    isRunning = false;
    isSpeedTesting = false;
    btnStop.disabled = true;
    btnStart.disabled = false;
    stopTimer(0);

    const aliveCount = items.filter(i => i.status === 'alive').length;
    btnSpeedTest.disabled = (aliveCount === 0);
    btnShareMobile.disabled = (aliveCount === 0);
    saveSession();
  };

  // --- Замер скорости ---

  btnSpeedTest.onclick = async () => {
    let aliveItems = items.filter(i => i.status === 'alive');
    if (aliveItems.length === 0) return alert('Нет живых ключей для замера!');

    isSpeedTesting = true;
    btnSpeedTest.disabled = true;
    btnStart.disabled = true;
    btnStop.disabled = false;

    let queue = [...aliveItems];
    const speedConcurrency = 2;

    const runSpeedWorker = async () => {
      while (queue.length > 0 && isSpeedTesting) {
        const item = queue.shift();
        item.status = 'speedtesting';
        renderItemStatus(item);

        try {
          const res = await fetch('/api/speed', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ raw: item.raw, timeout: 8000 })
          });
          const result = await res.json();
          item.status = 'alive';
          item.speed = result.ok ? result.speed : 'N/A';
        } catch (e) {
          item.status = 'alive';
          item.speed = 'Err';
        }
        renderItemStatus(item);
        renderWorkingList();
      }
    };

    const workers = [];
    for (let i = 0; i < speedConcurrency; i++) workers.push(runSpeedWorker());

    await Promise.all(workers);
    isSpeedTesting = false;
    btnSpeedTest.disabled = false;
    btnStart.disabled = false;
    btnStop.disabled = true;

    saveSession();
    renderWorkingList();
  };

  // --- Сортировка и рендер проверенных узлов ---

  function getSortedAndFilteredAliveItems() {
    const filter = filterInput.value.toLowerCase();
    const selCountry = countryFilter.value;
    const sortMode = sortSelect.value;

    let alive = items.filter(i => i.status === 'alive' || i.status === 'speedtesting');

    if (selCountry !== 'ALL') {
      alive = alive.filter(i => (i.country || 'UNKNOWN') === selCountry);
    }

    if (filter) {
      alive = alive.filter(i => 
        i.remark.toLowerCase().includes(filter) || 
        i.raw.toLowerCase().includes(filter) ||
        (i.country && i.country.toLowerCase().includes(filter))
      );
    }

    return alive.sort((a, b) => {
      if (sortMode === 'ping_asc') {
        const pa = typeof a.latency === 'number' ? a.latency : 99999;
        const pb = typeof b.latency === 'number' ? b.latency : 99999;
        return pa - pb;
      } else if (sortMode === 'speed_desc') {
        const sa = typeof a.speed === 'number' ? a.speed : -1;
        const sb = typeof b.speed === 'number' ? b.speed : -1;
        return sb - sa;
      } else if (sortMode === 'country_asc') {
        return (a.country || '').localeCompare(b.country || '');
      } else if (sortMode === 'proto_asc') {
        return (a.type || '').localeCompare(b.type || '');
      }
      return 0;
    });
  }

  function renderWorkingList() {
    const workingBox = document.getElementById('workingList');
    const sortedAlive = getSortedAndFilteredAliveItems();

    if (sortedAlive.length === 0) {
      workingBox.innerHTML = '<div class="empty-state">Нет узлов, подходящих под критерии.</div>';
      document.getElementById('workingCount').innerText = '0';
      return;
    }

    workingBox.innerHTML = sortedAlive.map(it => {
      let speedBadge = '';
      if (it.status === 'speedtesting') speedBadge = `<span class="badge badge-speed">Тест...</span>`;
      else if (it.speed !== null && it.speed !== undefined) speedBadge = `<span class="badge badge-speed">${it.speed} Mbps</span>`;

      const protoClass = getProtoClass(it.type);
      const flag = getCountryFlag(it.country);

      return `
        <div class="item-row" id="work-row-${it.id}">
          <div class="item-title" title="${escapeHtml(it.raw)}">
            <span class="badge badge-country" title="${escapeHtml(it.country)}">${flag} ${escapeHtml(it.country || '??')}</span>
            <span class="badge badge-proto ${protoClass}">${escapeHtml(it.type)}</span>
            <span>${escapeHtml(it.remark)}</span>
          </div>
          <div class="item-status">
            ${speedBadge}
            <span class="badge badge-alive">${it.latency !== null ? it.latency + ' ms' : 'OK'}</span>
          </div>
        </div>
      `;
    }).join('');

    document.getElementById('workingCount').innerText = sortedAlive.length;
  }

  sortSelect.onchange = () => {
    saveSettingsToStorage();
    renderWorkingList();
  };

  countryFilter.onchange = () => {
    renderWorkingList();
    renderAllGroups();
  };

  btnToggleAll.onclick = () => {
    allSpoilersOpen = !allSpoilersOpen;
    document.querySelectorAll('details.group-box').forEach(d => d.open = allSpoilersOpen);
  };

  function updateStats() {
    const total = items.length;
    const checked = items.filter(i => i.status !== 'idle' && i.status !== 'checking').length;
    const alive = items.filter(i => i.status === 'alive').length;
    const dead = items.filter(i => i.status === 'dead').length;

    document.getElementById('statTotal').innerText = total;
    document.getElementById('statChecked').innerText = checked;
    document.getElementById('statAlive').innerText = alive;
    document.getElementById('statDead').innerText = dead;

    btnShareMobile.disabled = (alive === 0);

    const totalGroups = Math.ceil(items.length / CHUNK_SIZE);
    for (let g = 0; g < totalGroups; g++) {
      const gSummary = document.getElementById(`group-stat-${g}`);
      if (gSummary) {
        const chunk = items.filter(i => i.group === g);
        const gAlive = chunk.filter(i => i.status === 'alive').length;
        const gDead = chunk.filter(i => i.status === 'dead').length;
        gSummary.innerText = `Живых: ${gAlive} · Мертвых: ${gDead}`;
      }
    }
  }

  function renderAllGroups() {
    const filter = filterInput.value.toLowerCase();
    const selCountry = countryFilter.value;
    const allBox = document.getElementById('allList');
    allBox.innerHTML = '';

    const totalGroups = Math.ceil(items.length / CHUNK_SIZE);
    if (totalGroups === 0) {
      allBox.innerHTML = '<div class="empty-state">Список узлов пуст</div>';
      document.getElementById('allCount').innerText = '0';
      return;
    }

    let renderedCount = 0;
    const fragment = document.createDocumentFragment();

    for (let g = 0; g < totalGroups; g++) {
      const allChunk = items.filter(i => i.group === g);
      let chunk = allChunk;

      if (selCountry !== 'ALL') {
        chunk = chunk.filter(i => (i.country || 'UNKNOWN') === selCountry);
      }
      if (filter) {
        chunk = chunk.filter(i => 
          i.remark.toLowerCase().includes(filter) || 
          i.raw.toLowerCase().includes(filter) ||
          (i.country && i.country.toLowerCase().includes(filter))
        );
      }
      
      if (chunk.length === 0 && (filter || selCountry !== 'ALL')) continue;
      renderedCount += chunk.length;

      const startIdx = g * CHUNK_SIZE + 1;
      const endIdx = startIdx + allChunk.length - 1;

      const details = document.createElement('details');
      details.className = 'group-box';
      details.id = `group-${g}`;
      if (allSpoilersOpen) details.open = true;

      const isChecked = selectedGroups.has(g);

      const summary = document.createElement('summary');
      summary.innerHTML = `
        <label style="display:inline-flex; align-items:center; gap:8px; cursor:pointer;" onclick="event.stopPropagation();">
          <input type="checkbox" class="group-select-chk" data-group="${g}" ${isChecked ? 'checked' : ''} onchange="toggleGroupSelection(${g}, this.checked)">
          <span>Группа #${g + 1} (${startIdx} - ${endIdx})</span>
        </label>
        <span class="group-tag" id="group-stat-${g}">Живых: 0 · Мертвых: 0</span>
      `;
      details.appendChild(summary);

      const content = document.createElement('div');
      content.className = 'group-content';
      content.innerHTML = chunk.map(createRowHTML).join('');
      details.appendChild(content);

      fragment.appendChild(details);
    }

    document.getElementById('allCount').innerText = renderedCount;
    allBox.appendChild(fragment);
  }

  function getProtoClass(type) {
    type = (type || '').toLowerCase();
    if (type.includes('vless')) return 'vless';
    if (type.includes('vmess')) return 'vmess';
    if (type.includes('hysteria') || type.includes('hy2')) return 'hysteria2';
    if (type.includes('trojan')) return 'trojan';
    if (type.includes('shadowsocks') || type.includes('ss')) return 'shadowsocks';
    return '';
  }

  function createRowHTML(it) {
    let badge = `<span class="badge badge-queued">В очереди</span>`;
    if (it.status === 'checking') badge = `<span class="badge badge-checking">Тест...</span>`;
    if (it.status === 'speedtesting') badge = `<span class="badge badge-speed">Скорость...</span>`;
    if (it.status === 'alive') {
      badge = `<span class="badge badge-alive">${it.latency} ms</span>`;
      if (it.speed !== null && it.speed !== undefined) badge += ` <span class="badge badge-speed">${it.speed} Mbps</span>`;
    }
    if (it.status === 'dead') badge = `<span class="badge badge-dead" title="${escapeHtml(it.error || 'Dead')}">${escapeHtml(it.error || 'Dead')}</span>`;

    const protoClass = getProtoClass(it.type);
    const flag = getCountryFlag(it.country);

    return `
      <div class="item-row" id="row-${it.id}">
        <div class="item-title" title="${escapeHtml(it.raw)}">
          <span class="badge badge-country" title="${escapeHtml(it.country)}">${flag} ${escapeHtml(it.country || '??')}</span>
          <span class="badge badge-proto ${protoClass}">${escapeHtml(it.type)}</span>
          <span>${escapeHtml(it.remark)}</span>
        </div>
        <div class="item-status">${badge}</div>
      </div>
    `;
  }

  function renderItemStatus(item) {
    const row = document.getElementById(`row-${item.id}`);
    if (row) {
      let badge = '';
      if (item.status === 'checking') badge = `<span class="badge badge-checking">Тест...</span>`;
      else if (item.status === 'speedtesting') badge = `<span class="badge badge-speed">Скорость...</span>`;
      else if (item.status === 'alive') {
        badge = `<span class="badge badge-alive">${item.latency} ms</span>`;
        if (item.speed !== null && item.speed !== undefined) badge += ` <span class="badge badge-speed">${item.speed} Mbps</span>`;
      }
      else if (item.status === 'dead') badge = `<span class="badge badge-dead" title="${escapeHtml(item.error || 'Dead')}">${escapeHtml(item.error || 'Dead')}</span>`;
      row.lastElementChild.innerHTML = badge;
    }
  }

  filterInput.oninput = () => {
    renderAllGroups();
    renderWorkingList();
  };

  btnClearDead.onclick = () => {
    items = items.filter(i => i.status !== 'dead');
    updateStats();
    renderAllGroups();
    renderWorkingList();
    saveSession();
  };

  btnCopyAlive.onclick = () => {
    const NL = String.fromCharCode(10);
    const sortedAlive = getSortedAndFilteredAliveItems().map(i => i.raw).join(NL);
    if (!sortedAlive) return alert('Рабочих ключей пока нет');
    navigator.clipboard.writeText(sortedAlive);
    alert('Скопировано в буфер: ' + sortedAlive.split(NL).length + ' шт.');
  };

  btnExportAlive.onclick = () => {
    const NL = String.fromCharCode(10);
    const sortedAlive = getSortedAndFilteredAliveItems().map(i => i.raw).join(NL);
    if (!sortedAlive) return alert('Рабочих ключей пока нет');
    const blob = new Blob([sortedAlive], { type: 'text/plain;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'working_vpn_keys.txt';
    a.click();
  };

  // --- Экспорт на смартфон ---

  const qrModal = document.getElementById('qrModal');
  const modalClose = document.getElementById('modalClose');
  const qrcodeContainer = document.getElementById('qrcode');
  const qrLinkText = document.getElementById('qrLinkText');
  const tabWifi = document.getElementById('tabWifi');
  const tabCloud = document.getElementById('tabCloud');

  function updateQRCode(url) {
    qrcodeContainer.innerHTML = '';
    qrLinkText.innerText = url;
    new QRCode(qrcodeContainer, {
      text: url,
      width: 190,
      height: 190,
      colorDark : "#000000",
      colorLight : "#ffffff",
      correctLevel : QRCode.CorrectLevel.M
    });
  }

  tabWifi.onclick = () => {
    tabWifi.classList.add('active');
    tabCloud.classList.remove('active');
    if (localShareUrl) updateQRCode(localShareUrl);
  };

  tabCloud.onclick = () => {
    if (!cloudShareUrl) return alert('Облачная ссылка dpaste недоступна.');
    tabCloud.classList.add('active');
    tabWifi.classList.remove('active');
    updateQRCode(cloudShareUrl);
  };

  modalClose.onclick = () => qrModal.classList.remove('active');
  qrModal.onclick = (e) => { if (e.target === qrModal) qrModal.classList.remove('active'); };

  btnShareMobile.onclick = async () => {
    const aliveKeys = getSortedAndFilteredAliveItems().map(i => i.raw);
    if (aliveKeys.length === 0) return alert('Нет рабочих ключей для экспорта!');

    btnShareMobile.disabled = true;
    btnShareMobile.innerText = 'Создание...';

    try {
      const res = await fetch('/api/share', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ keys: aliveKeys })
      });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error);

      localShareUrl = data.local_url;
      cloudShareUrl = data.cloud_url || "";

      tabWifi.classList.add('active');
      tabCloud.classList.remove('active');
      tabCloud.style.display = cloudShareUrl ? 'block' : 'none';

      updateQRCode(localShareUrl);
      qrModal.classList.add('active');
    } catch (err) {
      alert('Ошибка при подготовке ссылки: ' + err.message);
    } finally {
      btnShareMobile.disabled = false;
      btnShareMobile.innerText = '📱 QR на телефон';
    }
  };

  // Автозагрузка сессии при открытии
  window.addEventListener('DOMContentLoaded', () => {
    loadSession();
  });
</script>

</body>
</html>"""

async def handle_index(request):
    return web.Response(text=HTML_CONTENT, content_type='text/html')

async def handle_favicon(request):
    return web.Response(status=204)

app = web.Application()
app['alive_keys_cache'] = []

app.router.add_get('/', handle_index)
app.router.add_get('/favicon.ico', handle_favicon)
app.router.add_get('/sub', handle_sub)
app.router.add_post('/api/parse', handle_parse)
app.router.add_post('/api/ping', handle_ping)
app.router.add_post('/api/ping_batch', handle_ping_batch)
app.router.add_post('/api/speed', handle_speed)
app.router.add_post('/api/share', handle_share)

def find_available_port(start_port, max_attempts=15):
    for p in range(start_port, start_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((HOST, p))
                return p
            except OSError:
                continue
    return start_port

if __name__ == '__main__':
    try:
        ensure_singbox()
        ensure_geoip()
        lan_ip = get_lan_ip()
        PORT = find_available_port(PORT)
        
        print("\n" + "="*58)
        print("  [+] CoreProxy Lab сервер успешно запущен!")
        print(f"  --> Локальный интерфейс : http://localhost:{PORT}")
        print(f"  --> Домашняя сеть (Wi-Fi): http://{lan_ip}:{PORT}")
        print(f"  --> Эндпоинт подписки   : http://{lan_ip}:{PORT}/sub")
        print("  [*] Для остановки нажмите Ctrl + C")
        print("="*58 + "\n")
        
        web.run_app(app, host=HOST, port=PORT, print=None)
    except Exception as e:
        print(f"\n[Критическая ошибка]: {e}")
        input("\nНажмите Enter для завершения...")