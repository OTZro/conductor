"""Unit tests for the Monitor-tab metrics parser (pure, no subprocess)."""

from conductor.plugins.monitor.metrics import _size_mb, parse_metrics

SAMPLE = """14
38654705664
{ 3.25 2.90 2.87 }
{ sec = 1751900000, usec = 0 } Mon Jul  7 10:00:00 2026
---
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               50000.
Pages active:                            500000.
Pages inactive:                          400000.
Pages wired down:                        200000.
Pages occupied by compressor:            100000.
---
Filesystem     1024-blocks      Used Available Capacity iused ifree %iused  Mounted on
/dev/disk3s5     971350180 500000000 400000000    56%    1000  4000   20%   /System/Volumes/Data
---
Now drawing from 'AC Power'
 -InternalBattery-0 (id=6094947)\t87%; charging; 1:23 remaining present: true
---
conductor-abc12345
conductor-def67890
other-ticket-xyz
main
---
total = 2048.00M  used = 512.00M  free = 1536.00M  (encrypted)
---
7
"""


def test_parse_full_sample():
    d = parse_metrics(SAMPLE, now=1751900000 + 3600)
    assert d["cores"] == 14
    assert d["load"] == [3.25, 2.90, 2.87]
    assert d["cpu_pct"] == round(3.25 / 14 * 100, 1)
    assert d["uptime_s"] == 3600
    # mem: (active 500k + wired 200k + compressor 100k) pages * 16384
    assert d["mem"]["used"] == 800000 * 16384
    assert d["mem"]["total"] == 38654705664
    assert 0 < d["mem"]["pct"] < 100
    assert d["disk"]["mount"] == "/System/Volumes/Data"
    assert d["disk"]["pct"] == round(500000000 / 971350180 * 100, 1)
    assert d["battery"] == {"pct": 87, "state": "charging", "ac": True}
    assert d["sessions"] == {"conductor": 2, "tmux_other": 2, "claude": 7}
    assert d["swap"] == {"total_mb": 2048.0, "used_mb": 512.0}


def test_parse_degrades_on_empty_sections():
    d = parse_metrics("---\n---\n---\n---\n---\n---\n")
    assert d["cores"] == 0 and d["cpu_pct"] == 0.0
    assert d["battery"] is None
    assert d["sessions"]["claude"] == 0
    assert d["disk"]["total"] == 0


def test_size_mb_units():
    assert _size_mb("2048.00M") == 2048.0
    assert _size_mb("3.50G") == 3584.0
    assert _size_mb("junk") == 0.0
