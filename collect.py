#!/usr/bin/env python3
"""Run five rounds of the seven FFT placements, one process at a time."""

import csv
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess
import sys
import time


HERE = Path(__file__).resolve().parent
FFT = HERE.parents[1]
WORKSPACE = FFT.parent
MASKS = [0, 1, 2, 4, 8, 16, 31]
FIELDS = ['class', 'mask', 'repeat', 'loop_wall_s', 'total_cpu_s',
          'process_wall_s', 'verification', 'started_utc', 'log']


def run(cls, mask, label):
    log = HERE / 'logs' / f'{cls}-{label}-mask{mask}.log'
    command = [str(WORKSPACE / 'run'), str(FFT / 'ft.native'), cls]
    started = datetime.now(timezone.utc).isoformat()
    env = dict(os.environ, FT_FAR=str(mask))
    begin = time.perf_counter()
    with log.open('x') as output:
        result = subprocess.run(command, env=env, stdout=output,
                                stderr=subprocess.STDOUT)
    elapsed = time.perf_counter() - begin
    text = log.read_text()
    if result.returncode or 'Verification: SUCCESSFUL' not in text:
        raise RuntimeError(f'FFT failed; see {log}')
    if re.search(r'mbind:|set_mempolicy:|Operation not permitted', text):
        raise RuntimeError(f'NUMA warning; see {log}')
    loop = float(re.search(r'Loop wall time: ([0-9.]+) s', text)[1])
    cpu = float(re.search(r'Total CPU time: ([0-9.]+) s', text)[1])
    print(f'{cls} {label:8s} mask={mask:2d} loop={loop:.6f}s '
          f'process={elapsed:.3f}s verified', flush=True)
    return dict(zip(FIELDS, [cls, mask, label, loop, cpu, elapsed,
                            'SUCCESSFUL', started, str(log.relative_to(HERE))]))


def main():
    cls = sys.argv[1]
    if cls not in ('A', 'B'):
        raise ValueError('Choose A or B')
    path = HERE / 'measurements.csv'
    if path.exists():
        with path.open() as source:
            if any(row['class'] == cls for row in csv.DictReader(source)):
                raise RuntimeError(f'{cls} already has measurements in {path}')
    run(cls, 0, 'warmup')
    new_file = not path.exists()
    with path.open('a', newline='') as output:
        writer = csv.DictWriter(output, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        for repeat in range(1, 6):
            offset = repeat - 1
            order = MASKS[offset:] + MASKS[:offset]
            print(f'Round {repeat}: {order}', flush=True)
            for mask in order:
                row = run(cls, mask, f'run{repeat}')
                row['repeat'] = repeat
                writer.writerow(row)
                output.flush()


if __name__ == '__main__':
    main()
