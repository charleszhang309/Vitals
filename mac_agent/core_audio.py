"""Small Core Audio HAL adapter for the active output's volume and mute."""

import ctypes as C


def fourcc(value):
    return int.from_bytes(value.encode("ascii"), "big")


class PropertyAddress(C.Structure):
    _fields_ = [("selector", C.c_uint32), ("scope", C.c_uint32), ("element", C.c_uint32)]


class AudioPropertyError(RuntimeError):
    pass


class CoreAudio:
    def __init__(self):
        library = C.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        self.get = library.AudioObjectGetPropertyData
        self.get.argtypes = [C.c_uint32, C.POINTER(PropertyAddress), C.c_uint32, C.c_void_p,
                             C.POINTER(C.c_uint32), C.c_void_p]
        self.get.restype = C.c_int32
        self.set = library.AudioObjectSetPropertyData
        self.set.argtypes = [C.c_uint32, C.POINTER(PropertyAddress), C.c_uint32, C.c_void_p,
                             C.c_uint32, C.c_void_p]
        self.set.restype = C.c_int32

    def read(self, device, selector, scope, value):
        address = PropertyAddress(fourcc(selector), fourcc(scope), 0)
        size = C.c_uint32(C.sizeof(value))
        status = self.get(device, C.byref(address), 0, None, C.byref(size), C.byref(value))
        if status != 0:
            raise AudioPropertyError(f"Core Audio 读取 {selector} 失败（{status}）")
        return value.value

    def write(self, device, selector, value):
        address = PropertyAddress(fourcc(selector), fourcc("outp"), 0)
        status = self.set(device, C.byref(address), 0, None, C.sizeof(value), C.byref(value))
        if status != 0:
            raise AudioPropertyError(f"Core Audio 设置 {selector} 失败（{status}）")

    def volume(self, *, percent=None, muted=None):
        device = self.read(1, "dOut", "glob", C.c_uint32())
        if percent is not None:
            self.write(device, "vmvc", C.c_float(percent / 100))
        if muted is not None:
            self.write(device, "mute", C.c_uint32(muted))
        level = self.read(device, "vmvc", "outp", C.c_float())
        is_muted = self.read(device, "mute", "outp", C.c_uint32())
        return {"percent": round(level * 100), "muted": bool(is_muted)}
