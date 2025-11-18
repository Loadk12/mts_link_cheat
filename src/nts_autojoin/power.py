import ctypes, psutil
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040
def keep_awake(enable: bool = True):
    try:
        if enable:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED|ES_AWAYMODE_REQUIRED)
        else:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:
        pass
def on_ac_power() -> bool:
    try:
        b = psutil.sensors_battery()
        if b is None: return True
        return bool(b.power_plugged)
    except Exception:
        return True
