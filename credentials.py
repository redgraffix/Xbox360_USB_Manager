"""FTP secrets live in Windows Credential Manager, never in settings or logs."""
import ctypes
from ctypes import wintypes as w
import hashlib
import os


class Credential(ctypes.Structure):
    _fields_ = [('Flags', w.DWORD), ('Type', w.DWORD), ('TargetName', w.LPWSTR),
                ('Comment', w.LPWSTR), ('LastWritten', w.FILETIME),
                ('CredentialBlobSize', w.DWORD), ('CredentialBlob', ctypes.POINTER(ctypes.c_byte)),
                ('Persist', w.DWORD), ('AttributeCount', w.DWORD), ('Attributes', ctypes.c_void_p),
                ('TargetAlias', w.LPWSTR), ('UserName', w.LPWSTR)]


def key(config):
    text = f"{config['host'].casefold()}:{config.get('port',21)}:{config.get('user','')}"
    return 'Xbox360USBManager/FTP/' + hashlib.sha256(text.encode()).hexdigest()


def api():
    if os.name != 'nt':
        raise RuntimeError('Saved FTP passwords currently require Windows Credential Manager')
    dll = ctypes.WinDLL('Advapi32', use_last_error=True)
    dll.CredWriteW.argtypes = [ctypes.POINTER(Credential), w.DWORD]
    dll.CredWriteW.restype = w.BOOL
    dll.CredReadW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.POINTER(Credential))]
    dll.CredReadW.restype = w.BOOL
    dll.CredFree.argtypes = [ctypes.c_void_p]
    return dll


def save(config, secret):
    data = secret.encode('utf-16-le')
    if len(data) > 2560: raise ValueError('FTP password is too long')
    blob = (ctypes.c_byte * len(data)).from_buffer_copy(data)
    cred = Credential(Type=1, TargetName=key(config), CredentialBlobSize=len(data),
                      CredentialBlob=blob, Persist=2, UserName=config.get('user',''))
    if not api().CredWriteW(ctypes.byref(cred), 0):
        raise OSError('Windows could not save the FTP credential')


def read(config):
    dll = api(); result = ctypes.POINTER(Credential)()
    if not dll.CredReadW(key(config), 1, 0, ctypes.byref(result)):
        if ctypes.get_last_error() == 1168: return None
        raise OSError('Windows could not read the FTP credential')
    try:
        return ctypes.string_at(result.contents.CredentialBlob,
                                result.contents.CredentialBlobSize).decode('utf-16-le')
    finally: dll.CredFree(result)
