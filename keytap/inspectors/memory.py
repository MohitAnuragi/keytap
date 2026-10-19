import re

from keytap.core.utils import adb_shell

_ACCENT = "#00c864"
_FG_DIM = "#888888"


def adj_label(adj):
    if adj is None:  return ""
    if adj <= 0:     return "foreground"
    if adj <= 200:   return "visible"
    if adj <= 500:   return "service"
    if adj <= 700:   return "background"
    return "cached"


def compute_death_risk(adj, avail_pct):
    """Map oom_score_adj + system free % → (label, color)."""
    if adj <= 0:
        label, color = "Safe", _ACCENT
    elif adj <= 200:
        label, color = "Low", "#90ee90"
    elif adj <= 500:
        label, color = "Moderate", "#ffa040"
    elif adj <= 700:
        label, color = "High", "#ff6b35"
    else:
        label, color = "Critical", "#ff4444"
    if avail_pct < 10 and adj > 0:
        escalate = {
            "Safe":     ("Low",      "#90ee90"),
            "Low":      ("Moderate", "#ffa040"),
            "Moderate": ("High",     "#ff6b35"),
            "High":     ("Critical", "#ff4444"),
        }
        if label in escalate:
            label, color = escalate[label]
    return label, color


class MemoryFetcher:
    def __init__(self, serial=None):
        self._serial = serial

    def fetch(self, u2_dev=None):
        try:
            sys_out = adb_shell('cat /proc/meminfo', self._serial, u2_dev)
            meminfo = {}
            for line in sys_out.splitlines():
                if ':' in line:
                    k, v = line.split(':', 1)
                    nums = re.findall(r'\d+', v)
                    if nums:
                        meminfo[k.strip()] = int(nums[0])
            total_kb  = meminfo.get('MemTotal', 0)
            avail_kb  = meminfo.get('MemAvailable', meminfo.get('MemFree', 0))
            used_kb   = max(0, total_kb - avail_kb)
            used_pct  = (used_kb / total_kb * 100) if total_kb else 0.0
            avail_pct = (avail_kb / total_kb * 100) if total_kb else 100.0

            data = {
                'total_mb': total_kb // 1024,
                'avail_mb': avail_kb // 1024,
                'used_mb':  used_kb  // 1024,
                'used_pct': used_pct,
                'app_pkg':  '',
                'java_heap_used_mb':   0,
                'java_heap_total_mb':  0,
                'java_heap_pct':       None,
                'native_heap_mb':      0,
                'rss_mb':              0,
                'oom_adj':             None,
                'death_risk':          'Unknown',
                'death_risk_color':    _FG_DIM,
                'error':               None,
            }

            fg_out = adb_shell('dumpsys window | grep mCurrentFocus', self._serial, u2_dev)
            m = re.search(r'u\d+\s+([\w.]+)/', fg_out)
            if m:
                pkg = m.group(1)
                data['app_pkg'] = pkg
                meminfo_out = adb_shell(f'dumpsys meminfo {pkg}', self._serial, u2_dev)
                app = self._parse_app_meminfo(meminfo_out)
                data.update(app)
                if app.get('pid'):
                    adj_out = adb_shell(
                        f'cat /proc/{app["pid"]}/oom_score_adj', self._serial, u2_dev)
                    s = adj_out.strip()
                    if s.lstrip('-').isdigit():
                        adj = int(s)
                        data['oom_adj'] = adj
                        label, color = compute_death_risk(adj, avail_pct)
                        data['death_risk'] = label
                        data['death_risk_color'] = color

            return data
        except Exception as e:
            return {
                'error': str(e), 'total_mb': 0, 'avail_mb': 0,
                'used_mb': 0, 'used_pct': 0.0, 'avail_pct': 100.0,
                'app_pkg': '', 'java_heap_used_mb': 0, 'java_heap_total_mb': 0,
                'java_heap_pct': None, 'native_heap_mb': 0, 'rss_mb': 0,
                'oom_adj': None, 'death_risk': 'Unknown', 'death_risk_color': _FG_DIM,
            }

    def _parse_app_meminfo(self, text):
        result = {
            'pid': None,
            'java_heap_used_mb': 0, 'java_heap_total_mb': 0, 'java_heap_pct': None,
            'native_heap_mb': 0, 'rss_mb': 0,
        }
        for line in text.splitlines():
            m = re.search(r'MEMINFO in pid (\d+)', line)
            if m:
                result['pid'] = int(m.group(1))
                continue
            nums = re.findall(r'\d+', line)
            if not nums:
                continue
            s = line.strip()
            if s.startswith('Dalvik Heap') or s.startswith('Art Heap'):
                if len(nums) >= 7:
                    heap_size  = int(nums[5])
                    heap_alloc = int(nums[6])
                    if heap_size > 0:
                        result['java_heap_total_mb'] = heap_size  // 1024
                        result['java_heap_used_mb']  = heap_alloc // 1024
                        result['java_heap_pct']      = heap_alloc / heap_size * 100
            elif s.startswith('Native Heap'):
                result['native_heap_mb'] = int(nums[0]) // 1024
            elif re.match(r'TOTAL\b', s, re.IGNORECASE) or re.match(r'TOTAL\s+PSS', s, re.IGNORECASE):
                if len(nums) >= 5:
                    result['rss_mb'] = int(nums[4]) // 1024
                elif not result['rss_mb'] and nums:
                    result['rss_mb'] = int(nums[0]) // 1024
        return result
