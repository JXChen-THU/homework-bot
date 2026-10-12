"""sites.yaml 里账号密码的加密存储（Windows DPAPI）。

设计取舍
--------
用 Windows 自带的 DPAPI（CryptProtectData / CryptUnprotectData），密钥由
操作系统按当前 Windows 账户托管：

  - 不用额外记密码，程序能自己解开 → 不影响「开机后台自动抓取」
  - 别的 Windows 账户、别的电脑都解不开
  - 代价：sites.yaml 不再可移植（拷到别的机器上密码读不出来）

**这不是「防住本机恶意软件」级别的东西**：任何以你的身份运行的代码都能调用
DPAPI 解密。它的目标是「文件被看到时没有明文」，例如误传到网盘、
被同步工具上传、误提交进仓库。这一点在 README 里也写清楚了。

存储格式
--------
    enc:v1:<base64(DPAPI 密文)>

没有这个前缀的值一律按明文处理，因此旧配置可以无缝继续使用，
首次启动时会自动升级成密文（见 upgrade_plaintext）。
"""

import base64
import ctypes
import os
import sys

PREFIX = "enc:v1:"

# 额外的混淆熵：即使别人拿到 sites.yaml，单独调 DPAPI 也解不开，
# 必须同时知道这个应用专属的熵。
_ENTROPY = b"homework-bot/sites.yaml/v1"

_IS_WINDOWS = os.name == "nt"


def is_encrypted(value):
    """判断一个字段值是不是密文。"""
    return isinstance(value, str) and value.startswith(PREFIX)


def is_available():
    """当前环境能不能用 DPAPI。"""
    return _IS_WINDOWS


# ============================================================
# DPAPI 的 ctypes 封装
# ============================================================
class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob_from_bytes(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _bytes_from_blob(blob):
    return ctypes.string_at(blob.pbData, blob.cbData)


def _crypt32():
    # use_last_error=True 必须显式指定：否则 ctypes.get_last_error() 恒为 0，
    # 报错时拿不到真正的 Win32 错误码，日志会误导排错。
    return ctypes.WinDLL("crypt32", use_last_error=True)


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def _protect(data):
    """调用 CryptProtectData。返回 bytes 或抛异常。"""
    crypt32 = _crypt32()
    blob_in = _blob_from_bytes(data)
    blob_entropy = _blob_from_bytes(_ENTROPY)
    blob_out = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        None,
        ctypes.byref(blob_entropy),
        None, None, 0,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptProtectData 失败")
    try:
        return _bytes_from_blob(blob_out)
    finally:
        _kernel32().LocalFree(blob_out.pbData)


def _unprotect(data):
    """调用 CryptUnprotectData。返回 bytes 或抛异常。"""
    crypt32 = _crypt32()
    blob_in = _blob_from_bytes(data)
    blob_entropy = _blob_from_bytes(_ENTROPY)
    blob_out = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        ctypes.byref(blob_entropy),
        None, None, 0,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData 失败")
    try:
        return _bytes_from_blob(blob_out)
    finally:
        _kernel32().LocalFree(blob_out.pbData)


# ============================================================
# 对外接口
# ============================================================
def encrypt(plain):
    """把明文加密成 "enc:v1:..."。空值原样返回。"""
    if not plain:
        return plain or ""
    if is_encrypted(plain):
        return plain          # 已经是密文，不重复加密
    if not _IS_WINDOWS:
        raise RuntimeError("账号密码加密目前只支持 Windows")
    blob = _protect(plain.encode("utf-8"))
    return PREFIX + base64.b64encode(blob).decode("ascii")


def decrypt(value):
    """解密。返回 (明文, 是否成功)。

    - 明文/空值：原样返回，成功
    - 密文解不开（换了电脑或换了 Windows 账户）：返回 ("", False)
    """
    if not value:
        return "", True
    if not is_encrypted(value):
        return value, True

    if not _IS_WINDOWS:
        return "", False
    try:
        blob = base64.b64decode(value[len(PREFIX):])
        return _unprotect(blob).decode("utf-8"), True
    except Exception:
        return "", False


def looks_undecryptable(value):
    """是不是「有密文但解不开」——用来给用户明确提示。"""
    if not is_encrypted(value):
        return False
    return not decrypt(value)[1]
