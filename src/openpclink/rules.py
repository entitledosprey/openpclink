"""Runs PCLink's own rules engine, D_menu.dll, in a CPU emulator.

PCLink keeps its dependent-setting logic (what shows, what is checked, what changes
alongside what) in about 160,000 lines of x86 inside this DLL. Instead of translating
that by hand, it is executed from the user's own copy. Only its small exported
interface is used: this module answers its questions about parameters and records
what it asks to change. It cannot reach the ECU itself.
"""
from __future__ import annotations

import logging
import struct
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("openpclink.rules")

PROP_EXISTS = 1
PROP_VALUE = 2
PROP_VISIBLE = 10
FLAG_STARTUP = 0x20
FLAG_USER_CHANGE = 2
_SPECIAL_INIT = 0x69
_MAX_INSTRUCTIONS = 200_000_000

_STACK_TOP = 0x7F000000
_STACK_SIZE = 0x200000
_HEAP = 0x20000000
_HEAP_SIZE = 0x4000000
_GDT = 0x60000000
_TEB = 0x61000000
_STUB = 0x68000000
_RETURN_TRAP = _STUB + 0xFF00
_FLD_THUNK = _STUB + 0xFE00
_CALLBACKS = _STUB + 0x8000


class RulesError(Exception):
    """The rules engine could not run. PCLink's data-driven rules still apply."""


@dataclass(frozen=True)
class ValueChange:
    """A change the rules ask for, alongside one the user made."""

    data_id: int
    index: int
    value: float


class RulesEngine:
    def __init__(
        self,
        dll: bytes,
        read_value: Callable[[int, int], float],
        exists: Callable[[int], bool],
        default_visible: Callable[[int], bool],
    ) -> None:
        import pefile
        from unicorn import UC_ARCH_X86, UC_HOOK_CODE, UC_HOOK_MEM_UNMAPPED, UC_MODE_32, Uc, UcError
        from unicorn import x86_const as x86

        self._UcError = UcError
        self._x86 = x86
        self.read_value, self.exists, self.default_visible = read_value, exists, default_visible
        self.props: dict[tuple[int, int], int] = {}
        self.changes: list[ValueChange] = []
        self.messages: list[str] = []
        self.visibility_changed = False
        self._failure: str | None = None

        pe = pefile.PE(data=dll)
        self._base = pe.OPTIONAL_HEADER.ImageBase
        uc = self._uc = Uc(UC_ARCH_X86, UC_MODE_32)
        uc.mem_map(self._base, (pe.OPTIONAL_HEADER.SizeOfImage + 0xFFF) & ~0xFFF)
        uc.mem_write(self._base, pe.header)
        for section in pe.sections:
            uc.mem_write(self._base + section.VirtualAddress, section.get_data())
        uc.mem_map(_STACK_TOP - _STACK_SIZE, _STACK_SIZE)
        uc.reg_write(x86.UC_X86_REG_ESP, _STACK_TOP - 0x1000)
        uc.mem_map(_HEAP, _HEAP_SIZE)
        self._heap_top = _HEAP + 0x1000
        self._set_up_segments()
        self._stubs: dict[int, str] = {}
        self._handlers = self._windows_stubs()
        uc.mem_map(_STUB, 0x10000)
        for entry in pe.DIRECTORY_ENTRY_IMPORT:
            for item in entry.imports:
                address = self._stub_for((item.name or b"?").decode())
                uc.mem_write(item.address, struct.pack("<I", address))
        self._fld = self._alloc(16)
        uc.mem_write(_FLD_THUNK, b"\xdd\x05" + struct.pack("<I", self._fld) + b"\xc3")  # fld qword; ret
        self._callbacks: dict[int, Callable[[], None]] = {}
        uc.hook_add(UC_HOOK_CODE, self._on_stub, begin=_STUB, end=_STUB + 0xFFFF)
        uc.hook_add(UC_HOOK_MEM_UNMAPPED, self._on_fault)
        self._exports = {
            item.name.decode(): self._base + item.address
            for item in pe.DIRECTORY_ENTRY_EXPORT.symbols
            if item.name
        }
        self._call(self._base + pe.OPTIONAL_HEADER.AddressOfEntryPoint, stack=(0, 1, self._base))
        self._this = self._alloc(0x400)
        self._call(self._export("$bctr"), eax=self._this)
        next_callback = _CALLBACKS
        for name, handler in (
            ("SetSetIntProp", self._set_int),
            ("SetSetDoubleProp$", self._set_double),
            ("SetSetDoubleProp2", self._set_double2),
            ("SetSetStringProp", self._set_string),
            ("SetGetIntProp$", self._get_int),
            ("SetGetIntProp2", self._get_int2),
            ("SetGetDoubleProp$", self._get_double),
            ("SetGetDoubleProp2", self._get_double2),
            ("SetGetStringProp", self._get_string),
            ("SetMessageBox", self._message_box),
        ):
            self._callbacks[next_callback] = handler
            self._call(self._export(name), eax=self._this, stack=(0x1234, next_callback))
            next_callback += 16
        self._call(self._export("SpecialFunction"), eax=self._this, ecx=_SPECIAL_INIT, stack=(0, 0))

    # ---- what the application uses -----------------------------------------

    def value_changed(self, data_id: int, old: float, new: float, flags: int = FLAG_USER_CHANGE) -> list[ValueChange]:
        """Tell the rules a value changed. Returns the related changes they ask for."""
        self.changes = []
        old_low, old_high = struct.unpack("<II", struct.pack("<d", float(old)))
        new_low, new_high = struct.unpack("<II", struct.pack("<d", float(new)))
        self._call(
            self._export("ValueChanged"), eax=self._this, edx=data_id, ecx=flags,
            stack=(old_low, old_high, new_low, new_high),
        )
        return self.changes

    def visible(self, data_id: int) -> bool:
        return bool(self._property(data_id, PROP_VISIBLE))

    def take_messages(self) -> list[str]:
        messages, self.messages = self.messages, []
        return messages

    # ---- emulator plumbing --------------------------------------------------

    def _export(self, fragment: str) -> int:
        return next(address for name, address in self._exports.items() if "DLLInterface" in name and fragment in name)

    def _alloc(self, size: int, align: int = 16) -> int:
        address = (self._heap_top + align - 1) & ~(align - 1)
        if address + size > _HEAP + _HEAP_SIZE:
            raise RulesError("the rules engine ran out of memory")
        self._heap_top = address + size
        return address

    def _set_up_segments(self) -> None:
        x86, uc = self._x86, self._uc
        uc.mem_map(_GDT, 0x1000)
        uc.mem_map(_TEB, 0x4000)

        def segment(base: int, limit: int, access: int, flags: int) -> bytes:
            return struct.pack(
                "<Q",
                (limit & 0xFFFF) | ((base & 0xFFFFFF) << 16) | (access << 40)
                | (((limit >> 16) & 0xF) << 48) | (flags << 52) | (((base >> 24) & 0xFF) << 56),
            )

        uc.mem_write(_GDT + 8, segment(0, 0xFFFFF, 0x9B, 0xC))
        uc.mem_write(_GDT + 16, segment(0, 0xFFFFF, 0x93, 0xC))
        uc.mem_write(_GDT + 24, segment(_TEB, 0x3FFF, 0x93, 0x4))
        uc.mem_write(_GDT + 32, segment(0, 0xFFFFF, 0x93, 0xC))
        uc.reg_write(x86.UC_X86_REG_GDTR, (0, _GDT, 8 * 8 - 1, 0))
        uc.reg_write(x86.UC_X86_REG_SS, 4 << 3)
        uc.reg_write(x86.UC_X86_REG_DS, 2 << 3)
        uc.reg_write(x86.UC_X86_REG_ES, 2 << 3)
        uc.reg_write(x86.UC_X86_REG_FS, 3 << 3)
        uc.mem_write(_TEB, struct.pack("<I", 0xFFFFFFFF))
        uc.mem_write(_TEB + 4, struct.pack("<I", _STACK_TOP))
        uc.mem_write(_TEB + 8, struct.pack("<I", _STACK_TOP - _STACK_SIZE))
        uc.mem_write(_TEB + 0x18, struct.pack("<I", _TEB))

    def _u32(self, address: int) -> int:
        return struct.unpack("<I", self._uc.mem_read(address, 4))[0]

    def _w32(self, address: int, value: int) -> None:
        self._uc.mem_write(address, struct.pack("<I", value & 0xFFFFFFFF))

    def _sp(self) -> int:
        return self._uc.reg_read(self._x86.UC_X86_REG_ESP)

    def _arg(self, index: int) -> int:
        return self._u32(self._sp() + 4 + 4 * index)

    def _wstr(self, address: int) -> str:
        data = bytearray()
        while True:
            pair = self._uc.mem_read(address + len(data), 2)
            if pair == b"\0\0":
                return data.decode("utf-16le", "replace")
            data += pair

    def _astr(self, address: int) -> str:
        data = bytearray()
        while True:
            one = self._uc.mem_read(address + len(data), 1)
            if one == b"\0":
                return data.decode("latin-1")
            data += one

    def _stub_for(self, name: str) -> int:
        address = _STUB + len(self._stubs) * 8
        self._stubs[address] = name
        return address

    def _call(self, address: int, eax: int = 0, edx: int = 0, ecx: int = 0, stack: tuple[int, ...] = ()) -> int:
        x86, uc = self._x86, self._uc
        if self._failure:
            raise RulesError(self._failure)
        saved = self._sp()
        sp = saved
        for value in stack:
            sp -= 4
            self._w32(sp, value)
        sp -= 4
        self._w32(sp, _RETURN_TRAP)
        uc.reg_write(x86.UC_X86_REG_ESP, sp)
        uc.reg_write(x86.UC_X86_REG_EAX, eax)
        uc.reg_write(x86.UC_X86_REG_EDX, edx)
        uc.reg_write(x86.UC_X86_REG_ECX, ecx)
        try:
            uc.emu_start(address, _RETURN_TRAP, count=_MAX_INSTRUCTIONS)
        except self._UcError as exc:
            self._failure = f"rules engine stopped: {exc}"
        if self._failure:
            log.error(self._failure)
            raise RulesError(self._failure)
        result = uc.reg_read(x86.UC_X86_REG_EAX)
        uc.reg_write(x86.UC_X86_REG_ESP, saved)
        return result

    def _on_fault(self, _uc, _access, address, _size, _value, _data) -> bool:
        self._failure = f"rules engine read bad memory at {address:#x}"
        return False

    def _on_stub(self, uc, address, _size, _data) -> None:
        x86 = self._x86
        if address == _RETURN_TRAP or address == _FLD_THUNK:
            return
        callback = self._callbacks.get(address)
        if callback is not None:
            callback()
            return
        name = self._stubs.get(address)
        if name is None:
            return
        handler = self._handlers.get(name)
        if handler is None:
            self._failure = f"rules engine called unsupported Windows function {name}"
            uc.emu_stop()
            return
        count, function = handler
        result = function(*[self._arg(i) for i in range(count)]) or 0
        sp = self._sp()
        back = self._u32(sp)
        uc.reg_write(x86.UC_X86_REG_EAX, int(result) & 0xFFFFFFFF)
        uc.reg_write(x86.UC_X86_REG_ESP, sp + 4 + 4 * count)
        uc.reg_write(x86.UC_X86_REG_EIP, back)

    # ---- the DLL's callbacks ------------------------------------------------

    def _ids(self) -> tuple[int, int]:
        x86 = self._x86
        return self._uc.reg_read(x86.UC_X86_REG_EDX), self._uc.reg_read(x86.UC_X86_REG_ECX)

    def _return_int(self, value: int, stack_words: int) -> None:
        x86, uc = self._x86, self._uc
        sp = self._sp()
        back = self._u32(sp)
        uc.reg_write(x86.UC_X86_REG_EAX, value & 0xFFFFFFFF)
        uc.reg_write(x86.UC_X86_REG_ESP, sp + 4 + 4 * stack_words)
        uc.reg_write(x86.UC_X86_REG_EIP, back)

    def _return_double(self, value: float, stack_words: int) -> None:
        x86, uc = self._x86, self._uc
        uc.mem_write(self._fld, struct.pack("<d", float(value)))
        sp = self._sp()
        back = self._u32(sp)
        sp += 4 * stack_words
        self._w32(sp, back)
        uc.reg_write(x86.UC_X86_REG_ESP, sp)
        uc.reg_write(x86.UC_X86_REG_EIP, _FLD_THUNK)

    def _property(self, data_id: int, prop: int, index: int = 0) -> float:
        if prop == PROP_EXISTS:
            return data_id if self.exists(data_id) else -1
        if prop == PROP_VALUE:
            return self.read_value(data_id, index) if self.exists(data_id) else 0.0
        stored = self.props.get((data_id, prop))
        if stored is not None:
            return stored
        if prop == PROP_VISIBLE:
            return 1 if self.default_visible(data_id) else 0
        return 0

    def _get_int(self) -> None:
        data_id, prop = self._ids()
        self._return_int(int(self._property(data_id, prop)), 0)

    def _get_int2(self) -> None:
        data_id, prop = self._ids()
        self._return_int(int(self._property(data_id, prop, self._arg(0))), 2)

    def _get_double(self) -> None:
        data_id, prop = self._ids()
        self._return_double(self._property(data_id, prop), 0)

    def _get_double2(self) -> None:
        data_id, prop = self._ids()
        self._return_double(self._property(data_id, prop, self._arg(0)), 2)

    def _get_string(self) -> None:
        self._return_int(0, 2)

    def _set_int(self) -> None:
        data_id, prop = self._ids()
        value = self._arg(1)
        if prop == PROP_VISIBLE and self.props.get((data_id, prop)) != value:
            self.visibility_changed = True
        self.props[(data_id, prop)] = value
        self._return_int(0, 2)

    def _set_double(self) -> None:
        data_id, prop = self._ids()
        index = self._arg(0)
        value = struct.unpack("<d", self._uc.mem_read(self._sp() + 8, 8))[0]
        if prop == PROP_VALUE:
            if self.exists(data_id) and self.read_value(data_id, index) != value:
                self.changes.append(ValueChange(data_id, index, value))
        else:
            self.props[(data_id, prop)] = int(value)
        self._return_int(0, 3)

    def _set_double2(self) -> None:
        self._return_int(0, 5)

    def _set_string(self) -> None:
        self._return_int(0, 2)

    def _message_box(self) -> None:
        pointer = self._ids()[0]
        if pointer:
            self.messages.append(self._wstr(pointer))
        self._return_int(1, 1)

    # ---- the Windows functions it needs -------------------------------------

    def _windows_stubs(self) -> dict:
        uc, base = self._uc, self._base
        alloc, wstr, astr, u32, w32 = self._alloc, self._wstr, self._astr, self._u32, self._w32
        arg = self._arg  # noqa: F841
        BASE = base  # noqa: N806
        seen = []
        tls={}; tnext=[1]; err=[0]; seen=[]
        def tlsalloc(): tnext[0]+=1; return tnext[0]-1
        def m2w(cp,fl,src,n,dst,m):
            s=bytes(uc.mem_read(src,n)) if n>=0 else astr(src).encode('latin-1')+b'\0'
            w=s.decode('latin-1').encode('utf-16le')
            if dst and m: uc.mem_write(dst,w[:m*2])
            return len(w)//2
        def w2m(cp,fl,src,n,dst,m,*r):
            s=wstr(src)+'\0' if n==0xffffffff else bytes(uc.mem_read(src,n*2)).decode('utf-16le')
            b=s.encode('latin-1','replace')
            if dst and m: uc.mem_write(dst,b[:m])
            return len(b)
        def modname(h,buf,n):
            w="D_menu.dll\0".encode('utf-16le'); uc.mem_write(buf,w[:n*2]); return 10
        def modnameA(h,buf,n):
            uc.mem_write(buf,b"D_menu.dll\0"); return 10
        def sysinfo(p):
            uc.mem_write(p,struct.pack('<IIIIIIIII',0,4096,0x10000,0x7ffe0000,1,1,586,0x10000,0x60006)); return 0
        def cpinfo(cp,p): uc.mem_write(p,struct.pack('<I',1)+b'?\0'+bytes(12)); return 1
        def verex(p):
            uc.mem_write(p+4,struct.pack('<IIII',5,1,2600,2)); return 1
        def lcmap(*a): return 0
        def localeinfo(lcid,typ,buf,n):
            if n and buf: uc.mem_write(buf,b'\0\0')
            return 1
        def vquery(addr,buf,n):
            uc.mem_write(buf,struct.pack('<IIIIIII',addr&~0xfff,addr&~0xfff,4,0x10000,0x1000,4,0x20000)); return 28
        def valloc(a,n,t,p): return alloc((n+0xfff)&~0xfff,0x1000)
        def getproc(h,name):
            n=astr(name) if name>0xffff else str(name); seen.append(('GetProcAddress',n)); return 0
        sysstr={}
        def sysalloc(s,n):
            p=alloc(n*2+8)+4; w32(p-4,n*2)
            if s: uc.mem_write(p,bytes(uc.mem_read(s,n*2)))
            uc.mem_write(p+n*2,b'\0\0'); return p
        def sysrealloc(pp,s,n):
            w32(pp,sysalloc(s,n)); return 1
        def uni(hs): return 1
        K={'GetModuleHandleA':(1,lambda a:BASE),'GetModuleHandleW':(1,lambda a:BASE),'GetModuleFileNameW':(3,modname),'GetModuleFileNameA':(3,modnameA),
        'GetProcAddress':(2,getproc),'LoadLibraryA':(1,lambda a:0x70000000),'LoadLibraryExW':(3,lambda a,b,c:0x70000000),'FreeLibrary':(1,lambda a:1),
        'GetCommandLineW':(0,lambda:CMD),'GetStartupInfoW':(1,lambda p:0),'GetStartupInfoA':(1,lambda p:0),'GetVersion':(0,lambda:0x0a280105),'GetVersionExW':(1,verex),'GetVersionExA':(1,verex),
        'TlsAlloc':(0,tlsalloc),'TlsFree':(1,lambda i:1),'TlsGetValue':(1,lambda i:tls.get(i,0)),'TlsSetValue':(2,lambda i,v:tls.__setitem__(i,v) or 1),
        'LocalAlloc':(2,lambda f,n:alloc(n)),'LocalFree':(1,lambda p:0),'VirtualAlloc':(4,valloc),'VirtualFree':(3,lambda a,b,c:1),'VirtualProtect':(4,lambda a,b,c,d:1),'VirtualQuery':(3,vquery),'VirtualQueryEx':(4,lambda h,a,b,n:vquery(a,b,n)),
        'GetProcessHeap':(0,lambda:0x30000),'HeapCreate':(3,lambda a,b,c:0x30000),'HeapDestroy':(1,lambda h:1),'HeapAlloc':(3,lambda h,f,n:alloc(n)),'HeapFree':(3,lambda h,f,p:1),'HeapSize':(3,lambda h,f,p:0),
        'InitializeCriticalSection':(1,lambda p:0),'EnterCriticalSection':(1,lambda p:0),'LeaveCriticalSection':(1,lambda p:0),'DeleteCriticalSection':(1,lambda p:0),
        'InterlockedIncrement':(1,lambda p:(w32(p,u32(p)+1),u32(p))[1]),'InterlockedDecrement':(1,lambda p:(w32(p,u32(p)-1),u32(p))[1]),'InterlockedExchange':(2,lambda p,v:(lambda o:(w32(p,v),o)[1])(u32(p))),
        'GetACP':(0,lambda:1252),'GetOEMCP':(0,lambda:437),'GetCPInfo':(2,cpinfo),'GetCPInfoExW':(3,lambda a,b,c:0),'IsDBCSLeadByteEx':(2,lambda a,b:0),'MultiByteToWideChar':(6,m2w),'WideCharToMultiByte':(8,w2m),
        'GetCurrentThreadId':(0,lambda:1),'GetCurrentThread':(0,lambda:0xfffffffe),'GetCurrentProcess':(0,lambda:0xffffffff),'GetTickCount':(0,lambda:int(time.monotonic()*1000)&0xffffffff),'QueryPerformanceCounter':(1,lambda p:(uc.mem_write(p,struct.pack('<Q',int(time.monotonic()*1e6))),1)[1]),'QueryPerformanceFrequency':(1,lambda p:(uc.mem_write(p,struct.pack('<Q',1000000)),1)[1]),
        'GetSystemInfo':(1,sysinfo),'GetLastError':(0,lambda:err[0]),'SetLastError':(1,lambda v:err.__setitem__(0,v)),'IsDebuggerPresent':(0,lambda:0),'GetThreadLocale':(0,lambda:0x409),'SetThreadLocale':(1,lambda a:1),'GetUserDefaultLCID':(0,lambda:0x409),'GetUserDefaultUILanguage':(0,lambda:0x409),'GetSystemDefaultUILanguage':(0,lambda:0x409),'GetSystemDefaultLangID':(0,lambda:0x409),'IsValidLocale':(2,lambda a,b:1),'GetLocaleInfoW':(4,localeinfo),'GetLocaleInfoA':(4,localeinfo),
        'EnumCalendarInfoW':(4,lambda a,b,c,d:1),'EnumSystemLocalesW':(2,lambda a,b:1),'GetStdHandle':(1,lambda a:0),'GetFileType':(1,lambda a:0),'SetHandleCount':(1,lambda a:a),'SetConsoleCtrlHandler':(2,lambda a,b:1),'GetEnvironmentStringsW':(0,lambda:ENV),'FreeEnvironmentStringsW':(1,lambda p:1),
        'lstrlenW':(1,lambda p:len(wstr(p))),'VerSetConditionMask':(4,lambda a,b,c,d:0),'VerifyVersionInfoW':(4,lambda a,b,c,d:1),'Sleep':(1,lambda a:0),'SwitchToThread':(0,lambda:1),'FindFirstFileW':(2,lambda a,b:0xffffffff),'FindClose':(1,lambda a:1),'CloseHandle':(1,lambda a:1),'GetLocalTime':(1,lambda p:0),'RtlUnwind':(4,lambda a,b,c,d:0),
        'CoInitialize':(1,lambda a:0),'CoUninitialize':(0,lambda:0),'GetSystemMetrics':(1,lambda a:0),'CharNextW':(1,lambda p:p+2),'RegOpenKeyExW':(5,lambda *a:2),'RegCloseKey':(1,lambda a:0),'RegQueryValueExW':(6,lambda *a:2),'GetFileVersionInfoSizeW':(2,lambda a,b:0),'GetFileVersionInfoW':(4,lambda *a:0),'VerQueryValueW':(4,lambda *a:0),'NetWkstaGetInfo':(3,lambda *a:1),'NetApiBufferFree':(1,lambda a:0),
        'SysAllocStringLen':(2,sysalloc),'SysFreeString':(1,lambda a:0),'SysReAllocStringLen':(3,sysrealloc),'VariantInit':(1,lambda a:0),'VariantClear':(1,lambda a:0),'GetErrorInfo':(2,lambda a,b:1),'LoadStringW':(4,lambda a,b,c,d:0),'CharUpperBuffW':(2,lambda a,b:b),'CharLowerBuffW':(2,lambda a,b:b),'CharUpperW':(1,lambda a:a),'CompareStringW':(6,lambda *a:2),'GetStringTypeW':(4,lambda *a:1),'GetStringTypeA':(5,lambda *a:1),'LCMapStringA':(6,lcmap),'GetDateFormatW':(6,lambda *a:0),'FormatMessageW':(7,lambda *a:0),'EnumThreadWindows':(3,lambda a,b,c:1),'ExitThread':(1,lambda a:0)}

        CMD = alloc(8)  # noqa: N806
        ENV = alloc(8)  # noqa: N806
        K["GetProcAddress"] = (2, self._get_proc_address)
        # PCLink ships no borlndmm.dll, so this load fails on Windows too, and the
        # runtime then uses its built-in memory manager.
        K["GetModuleHandleA"] = (1, lambda name: 0 if name and self._is_absent(astr(name)) else BASE)
        K["LoadLibraryA"] = (1, lambda name: 0 if self._is_absent(astr(name)) else 0x70000000)
        for name, count in (("GetNativeSystemInfo", 1), ("GetLogicalProcessorInformation", 2), ("GetSystemTimes", 3), ("LocaleNameToLCID", 2)):
            K[name] = (count, lambda *_a: 0)
        K["MessageBoxW"] = (4, lambda *_a: 1)
        K["MessageBoxA"] = (4, lambda *_a: 1)
        K["RaiseException"] = (4, self._raise_exception)
        # Thread and event plumbing the runtime sets up but the rules never wait on.
        handles = iter(range(0x1000, 0x100000, 4))
        K.update(
            {
                "CreateEventW": (4, lambda *_a: next(handles)),
                "SetEvent": (1, lambda _h: 1),
                "ResetEvent": (1, lambda _h: 1),
                "WaitForSingleObject": (2, lambda *_a: 0),
                "MsgWaitForMultipleObjects": (5, lambda *_a: 0),
                "PeekMessageW": (5, lambda *_a: 0),
                "CreateThread": (6, lambda *_a: next(handles)),
                "ResumeThread": (1, lambda _h: 1),
                "SuspendThread": (1, lambda _h: 0),
                "GetExitCodeThread": (2, lambda *_a: 1),
                "GetThreadPriority": (1, lambda _h: 0),
                "SetThreadPriority": (2, lambda *_a: 1),
                "GetFullPathNameW": (4, lambda *_a: 0),
                "GetDiskFreeSpaceW": (5, lambda *_a: 0),
            }
        )
        return K

    @staticmethod
    def _is_absent(library: str) -> bool:
        return "borlndmm" in library.lower()

    def _get_proc_address(self, _handle: int, name: int) -> int:
        if name <= 0xFFFF:
            return 0
        wanted = self._astr(name)
        if wanted not in self._handlers:
            return 0
        for address, known in self._stubs.items():
            if known == wanted:
                return address
        return self._stub_for(wanted)

    def _raise_exception(self, code: int, _flags: int, _count: int, _args: int) -> int:
        # The runtime's "debugger, are you there?" notices are not real failures.
        if 0x0EEDFAD0 <= code <= 0x0EEDFAEF:
            return 0
        self._failure = f"rules engine raised exception {code:#x}"
        self._uc.emu_stop()
        return 0


__all__ = ["FLAG_STARTUP", "FLAG_USER_CHANGE", "RulesEngine", "RulesError", "ValueChange"]
