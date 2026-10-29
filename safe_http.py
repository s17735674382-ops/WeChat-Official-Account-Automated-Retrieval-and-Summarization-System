"""公开网页请求：拒绝本地/内网目标，校验重定向，不使用代理或 Cookie。"""

import ipaddress
import socket
from http.client import HTTPConnection, HTTPSConnection, HTTPException
from urllib.parse import urljoin, urlsplit, urlunsplit


class FetchError(Exception):
    pass


def is_public_ip(value):
    address = ipaddress.ip_address(value)
    return address.is_global and not address.is_multicast


def validate_url(url, allowed_host=None):
    """只校验格式；不查询 DNS，因此手动粘贴保存可以完全离线。"""
    if not isinstance(url, str) or not url.strip():
        raise FetchError("网址不能为空。")
    url = url.strip()
    if any(ord(c) <= 32 or ord(c) == 127 for c in url) or "\\" in url:
        raise FetchError("网址含有空白、控制字符或反斜杠。")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
        if parts.scheme not in ("http", "https") or not host:
            raise FetchError("仅允许完整的 http:// 或 https:// 网址。")
        if parts.username is not None or parts.password is not None:
            raise FetchError("网址不能包含用户名或密码。")
        if port not in (None, 80 if parts.scheme == "http" else 443):
            raise FetchError("仅允许 HTTP 的 80 端口和 HTTPS 的 443 端口。")
        host = host.encode("idna").decode("ascii").lower().rstrip(".")
    except (ValueError, UnicodeError) as exc:
        raise FetchError("网址格式无效。") from exc
    if allowed_host and host != allowed_host:
        raise FetchError("链接导入仅允许 mp.weixin.qq.com，重定向也不能离开该域名。")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise FetchError("不允许访问本机或内网地址。")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not is_public_ip(host):
        raise FetchError("不允许访问本地、内网或其他非公开 IP 地址。")
    return url, parts, host


def resolve_public(host, port):
    try:
        results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(result[4][0] for result in results))
    except OSError as exc:
        raise FetchError(f"无法解析网站地址：{exc}") from exc
    if not addresses or not all(is_public_ip(ip) for ip in addresses):
        raise FetchError("网站解析到本地、内网或非公开地址，已拒绝请求。")
    # 优先 IPv4；连接固定到已检查的 IP，避免连接时重新解析 DNS。
    return next((ip for ip in addresses if ":" not in ip), addresses[0])


class PublicHTTPConnection(HTTPConnection):
    def connect(self):
        self.sock = socket.create_connection((self.public_ip, self.port), self.timeout)


class PublicHTTPSConnection(HTTPSConnection):
    def connect(self):
        raw = socket.create_connection((self.public_ip, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def fetch_public(url, timeout=20, max_bytes=5 * 1024 * 1024, allowed_host=None):
    """最多跟随 3 次重定向；每一跳都检查目标并固定到已验证的 IP。"""
    for redirect in range(4):
        url, parts, host = validate_url(url, allowed_host)
        port = 443 if parts.scheme == "https" else 80
        ip = resolve_public(host, port)
        connection_type = PublicHTTPSConnection if parts.scheme == "https" else PublicHTTPConnection
        connection = connection_type(host, port, timeout=timeout)
        connection.public_ip = ip
        target = urlunsplit(("", "", parts.path or "/", parts.query, ""))
        try:
            connection.request("GET", target, headers={
                "User-Agent": "University-Information-Radar/0.3 (public reader)",
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/html, */*",
                "Accept-Encoding": "identity",
            })
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location or redirect == 3:
                    raise FetchError("重定向地址缺失或跳转次数过多。")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise FetchError(f"服务器返回 HTTP {response.status}，无法读取。")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise FetchError("服务器返回压缩内容，本版本不尝试解压，请改用手动粘贴。")
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise FetchError(f"下载内容超过 {max_bytes // (1024 * 1024)} MB 限制，已停止读取。")
            if not data.strip():
                raise FetchError("服务器返回空内容。")
            return data, response.headers, url
        except (OSError, HTTPException, ValueError, UnicodeError) as exc:
            raise FetchError(f"公开网络请求失败（含超时、证书或连接错误）：{exc}") from exc
        finally:
            connection.close()
    raise FetchError("重定向次数过多。")
