#!/usr/bin/env python3
"""Prepare an OCaml compiler and NUMA environment on a Linux experiment node."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit


ACTIVE = Path.home() / '.cache/ocaml-numa-setup.active'
BALANCING = Path('/proc/sys/kernel/numa_balancing')
BOOT_ID = Path('/proc/sys/kernel/random/boot_id')


def say(message):
    print(message, flush=True)


def ask(message, default):
    return input(f'{message} [{default}]: ').strip() or str(default)


def numbers(text):
    result = set()
    for part in text.strip().split(','):
        part = part.strip()
        if part:
            if not re.fullmatch(r'[0-9]+(?:-[0-9]+)?', part):
                raise ValueError('Use comma-separated IDs or ranges.')
            ends = list(map(int, part.split('-')))
            if ends[0] > ends[-1]:
                raise ValueError('Range start must not exceed its end.')
            result.update(range(ends[0], ends[-1] + 1))
    return result


def choose(message, default, choices):
    while True:
        answer = ask(message, default)
        if answer.isdecimal() and int(answer) in choices:
            return int(answer)
        say(f'Choose one of: {", ".join(map(str, sorted(choices)))}')


def choose_cpus(available):
    cpu_list = ','.join(map(str, sorted(available)))
    say(f'Available CPUs on the near node: {cpu_list}')
    modes = {1, 2} if len(available) > 1 else {1}
    mode = choose('Experiment type (1 = single CPU, 2 = multiple CPUs)', 1, modes)
    if mode == 1:
        return [choose('Workload CPU', min(available), available)]
    say('Use comma-separated CPU IDs or ranges, for example 0,2,4,6 or 0-3.')
    while True:
        try:
            cpus = numbers(ask('Workload CPUs', cpu_list))
        except ValueError:
            cpus = set()
        if len(cpus) >= 2 and cpus <= available:
            return sorted(cpus)
        say(f'Select at least two CPUs from: {cpu_list}')


def yes(message, default='Y'):
    while True:
        answer = ask(message, default).lower()
        if answer in ('y', 'yes', 'n', 'no'):
            return answer in ('y', 'yes')
        say('Enter y or n.')


def save(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    temporary.replace(path)


def run(command, *, cwd=None, log=None, env=None):
    if log:
        say(f'  {shlex.join(command)}  (log: {log})')
        with log.open('w') as output:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdout=output,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            try:
                result = process.wait()
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                process.wait()
                raise
        if result:
            say('\n'.join(log.read_text(errors='replace').splitlines()[-20:]))
            raise RuntimeError(f'Command failed ({result}); see {log}')
    else:
        subprocess.run(command, cwd=cwd, env=env, check=True)


def root_write(path, value):
    subprocess.run(['sudo', '-n', 'tee', str(path)], input=value + '\n',
                   text=True, stdout=subprocess.DEVNULL, check=True)
    if path.read_text().strip() != value:
        raise RuntimeError(f'Could not set {path} to {value}')


def check_repository(repo, branch):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    env.setdefault('GIT_SSH_COMMAND', 'ssh -o BatchMode=yes')
    command = ['git', 'ls-remote', '--exit-code', '--', repo, f'refs/heads/{branch}']
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    url = urlsplit(repo)
    if result.returncode not in (0, 2) and url.scheme == 'https' and url.hostname == 'github.com':
        say('Repository access failed; checking GitHub authentication.')
        auth = subprocess.run(['gh', 'auth', 'status', '--hostname', 'github.com'],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if auth.returncode:
            say('Open the displayed URL on your laptop and enter the code to sign in.')
            say('Use a GitHub account with access to this repository.')
            run(['gh', 'auth', 'login', '--hostname', 'github.com',
                 '--git-protocol', 'https', '--web'])
        run(['gh', 'auth', 'setup-git', '--hostname', 'github.com'])
        result = subprocess.run(command, env=env, capture_output=True, text=True)
    if result.returncode == 2:
        raise RuntimeError(f'Repository is accessible, but branch {branch!r} was not found.')
    if result.returncode:
        say(result.stderr.strip())
        raise RuntimeError('Cannot access the repository. Check the URL and repository permissions; '
                           'SSH URLs require working SSH authentication on this node.')
    say(f'Repository and branch {branch!r} are accessible.')
    return env


def restore(work):
    state_path = work / 'machine-state.json'
    state = json.loads(state_path.read_text())
    if not state['active']:
        say('Machine settings are already restored.')
        return
    if state['host'] != socket.gethostname() or state['boot_id'] != BOOT_ID.read_text().strip():
        raise RuntimeError('Saved state belongs to another host or boot; no settings changed.')
    if ACTIVE.exists() and Path(ACTIVE.read_text().strip()) != work:
        raise RuntimeError('Another workspace is active; restore that one first.')
    errors = []
    for name, original in state['settings'].items():
        path = Path(name)
        try:
            if path.read_text().strip() != original:
                root_write(path, original)
        except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
            errors.append(f'{path}: {error}')
    if errors:
        raise RuntimeError('Restoration incomplete:\n' + '\n'.join(errors))
    state['active'] = False
    save(state_path, state)
    if ACTIVE.exists():
        ACTIVE.unlink()
    say('Original CPU states and NUMA balancing restored.')


def show_ready(work, config):
    say(f'\nREADY: {work}')
    say('Compiler installed and NUMA settings applied.')
    say('Setup did not download or build a workload.')
    say(f'OCaml source and build: {work / "ocaml"}')
    say(f'Installed compiler: {work / "install/bin/ocamlopt"}')
    say(f'Native runtime library: {work / "install/lib/ocaml/libasmrun.a"}')
    say('\nNext steps: StreaML example')
    say('Run these commands in order in your current Bash terminal.')
    say('\n1. Load the environment:')
    say(f'  source {shlex.quote(str(work / "env.sh"))}')
    say('This defines WORKSPACE and selects the installed compiler.')
    say('\n2. Clone the workload repository:')
    say('Enter the StreaML repository URL when prompted.')
    say(f'git clone creates {work / "workload"} and downloads the repository into it.')
    say('You do not need to create that directory yourself. Skip this step if already cloned there.')
    say('  read -r -p "Workload repository URL: " WORKLOAD_REPO')
    say('  git clone "$WORKLOAD_REPO" "$WORKSPACE/workload"')
    say('\n3. Compile StreaML:')
    say('The source file is workload/streaml.ml; the executable will be streaml.native.')
    say('  ocamlopt -O3 -I +unix unix.cmxa "$WORKSPACE/workload/streaml.ml" -cclib -lnuma -o "$WORKSPACE/streaml.native"')
    say('The compiler links the matching OCaml runtime into streaml.native.')
    say('\n4. Run StreaML with all-near and all-far arrays:')
    say('  STREAM_N=1000000 STREAM_FAR=0 "$WORKSPACE/run" "$WORKSPACE/streaml.native"')
    say('  STREAM_N=1000000 STREAM_FAR=7 "$WORKSPACE/run" "$WORKSPACE/streaml.native"')
    say('Both small runs should print "Solution Validates".')
    cpu_list = ','.join(map(str, config['cpus']))
    say(f'run uses CPU list {cpu_list}, with near node {config["near"]} and far node {config["far"]}.')
    say('\nFor another workload, use its build instructions in step 3,')
    say('then launch its executable through "$WORKSPACE/run" in step 4.')
    say('\nRestore the original machine settings when finished:')
    say(f'  python3 {shlex.quote(str(work / "setup.py"))} --restore {shlex.quote(str(work))}')


def activate(work):
    config = json.loads((work / 'config.json').read_text())
    if config['host'] != socket.gethostname():
        raise RuntimeError('This workspace was prepared on another host.')
    if ACTIVE.exists():
        raise RuntimeError(f'An environment is already active: {ACTIVE.read_text().strip()}')
    if 'cpus' not in config:
        config['cpus'] = [config['cpu']]
    cpus = set(config['cpus'])
    online = numbers(Path('/sys/devices/system/cpu/online').read_text())
    if not cpus <= online & os.sched_getaffinity(0):
        raise RuntimeError('A selected workload CPU is no longer online and allowed.')
    if any(not Path(f'/sys/devices/system/cpu/cpu{cpu}/node{config["near"]}').exists()
           for cpu in cpus):
        raise RuntimeError('A selected CPU no longer belongs to the near node.')
    settings = {}
    if config['memory_only']:
        for cpu in config['far_cpus']:
            path = Path(f'/sys/devices/system/cpu/cpu{cpu}/online')
            if cpu in online:
                if not path.exists():
                    raise RuntimeError(f'CPU {cpu} cannot be offlined on this host.')
                settings[str(path)] = path.read_text().strip()
    settings[str(BALANCING)] = BALANCING.read_text().strip()
    state = dict(host=socket.gethostname(), boot_id=BOOT_ID.read_text().strip(),
                 active=True, settings=settings)
    save(work / 'machine-state.json', state)
    ACTIVE.write_text(str(work) + '\n')
    try:
        for name in settings:
            root_write(Path(name), '0')
        run(['numactl', '--hardware'], log=work / 'logs/topology-active.txt')
        (work / 'env.sh').write_text(
            f'export WORKSPACE={shlex.quote(str(work))}\n'
            'export PATH="$WORKSPACE/install/bin:$PATH"\n')
    except BaseException:
        restore(work)
        raise
    show_ready(work, config)


def prepare():
    if ACTIVE.exists():
        raise RuntimeError(f'Restore the active environment first: {ACTIVE.read_text().strip()}')
    say('OCaml NUMA environment setup')
    repo = ask('OCaml repository URL', 'https://github.com/HExSA-Lab/ocaml.git')
    url = urlsplit(repo)
    if url.scheme in ('http', 'https') and (url.username is not None or url.password is not None):
        raise RuntimeError('Enter a repository URL without credentials; GitHub login is handled separately.')
    branch = ask('Branch', 'STRATA-5.5.0')
    work = Path(ask('Workspace', Path.home() / time.strftime('ocaml-numa-%Y%m%d-%H%M%S'))).expanduser().resolve()
    if work.exists():
        raise RuntimeError('Choose a new workspace; existing directories are not overwritten.')
    work.mkdir(parents=True)
    logs = work / 'logs'
    logs.mkdir()
    say('\n[1/6] Dependencies')
    packages = ['build-essential', 'git', 'gh', 'numactl', 'libnuma-dev', 'pkg-config', 'libzstd-dev']
    missing = []
    for package in packages:
        result = subprocess.run(['dpkg-query', '-W', '-f=${Status}', package],
                                capture_output=True, text=True)
        if result.returncode or result.stdout != 'install ok installed':
            missing.append(package)
    if missing:
        say('Refreshing apt package lists and installing: ' + ', '.join(missing))
        run(['sudo', '-n', 'apt-get', 'update'], log=logs / 'apt-update.log')
        run(['sudo', '-n', 'apt-get', 'install', '-y', *missing], log=logs / 'apt-install.log')
    else:
        say('All required packages are already installed.')

    say('\n[2/6] NUMA topology')
    run(['numactl', '--hardware'])
    run(['lscpu', '-e=CPU,NODE,SOCKET,CORE,ONLINE'])
    run(['numactl', '--hardware'], log=logs / 'topology-original.txt')
    (logs / 'process-status.txt').write_text(Path('/proc/self/status').read_text())
    status = Path('/proc/self/status').read_text()
    allowed = numbers(re.search(r'^Mems_allowed_list:\s*(.+)$', status, re.M)[1])
    nodes = {}
    for path in Path('/sys/devices/system/node').glob('node[0-9]*'):
        node = int(path.name[4:])
        memory = re.search(r'MemTotal:\s+(\d+)', (path / 'meminfo').read_text())
        if node in allowed and memory and int(memory[1]) > 0:
            nodes[node] = numbers((path / 'cpulist').read_text())
    if len(nodes) < 2:
        raise RuntimeError('Two allowed NUMA nodes with memory are required.')
    say('\nThe OCaml runtime currently uses NUMA node 1 for far allocations.')
    say('Press Enter to keep node 1, or enter a different node ID.')
    far = choose('Far NUMA node', 1, nodes)
    near_choices = set(nodes) - {far}
    near = choose('Near NUMA node', min(near_choices), near_choices)
    online = numbers(Path('/sys/devices/system/cpu/online').read_text())
    near_cpus = nodes[near] & online & os.sched_getaffinity(0)
    if not near_cpus:
        raise RuntimeError('No online, allowed CPU belongs to the selected near node.')
    cpus = choose_cpus(near_cpus)
    cpu_list = ','.join(map(str, cpus))
    memory_only = yes('Offline the far node CPUs for a memory-only far node?')
    if memory_only:
        online = numbers(Path('/sys/devices/system/cpu/online').read_text())
        for c in nodes[far] & online:
            if not Path(f'/sys/devices/system/cpu/cpu{c}/online').exists():
                raise RuntimeError(f'CPU {c} cannot be offlined; rerun and keep far CPUs online.')
    jobs = min(8, len(os.sched_getaffinity(0)))
    say(f'\nRepository: {repo}\nBranch: {branch}\nWorkspace: {work}')
    say(f'Near: node {near}, CPUs {cpu_list}; far: node {far}; memory-only: {memory_only}')
    say(f'Build jobs: {jobs}; automatic NUMA balancing will be disabled.')
    if not yes('Build and prepare this configuration?'):
        say('Stopped before cloning or changing machine settings.')
        return

    say('\n[3/6] Source checkout')
    src = work / 'ocaml'
    git_env = check_repository(repo, branch)
    run(['git', 'clone', '--depth', '1', '--single-branch', '--branch', branch,
         '--', repo, str(src)], env=git_env, log=logs / 'clone.log')
    commit = subprocess.check_output(['git', '-C', str(src), 'rev-parse', 'HEAD'], text=True).strip()
    array_path = src / 'runtime/array.c'
    array_source = array_path.read_text()
    array_backend = re.search(r'^#define TARGET_FAR_NUMA_NODE[ \t]+(\d+)[ \t]*$', array_source, re.M)
    bigarray_backend = (src / 'runtime/tier_alloc.c').exists()
    if not array_backend and not bigarray_backend:
        raise RuntimeError('This branch has neither the STRATA Array nor tier_alloc Bigarray backend.')
    if array_backend and int(array_backend[1]) != far:
        array_path.write_text(array_source[:array_backend.start()] +
                              f'#define TARGET_FAR_NUMA_NODE {far}' + array_source[array_backend.end():])
    (logs / 'runtime-node.patch').write_text(subprocess.check_output(
        ['git', '-C', str(src), 'diff', '--', 'runtime/array.c'], text=True))
    config = dict(host=socket.gethostname(), repo=repo, branch=branch, commit=commit, near=near, far=far,
                  cpus=cpus, far_cpus=sorted(nodes[far]), memory_only=memory_only)
    save(work / 'config.json', config)
    shutil.copyfile(Path(__file__).resolve(), work / 'setup.py')

    say(f'\n[4/6] Build and install OCaml ({commit[:12]})')
    env = dict(os.environ, LIBS='-lnuma', LDFLAGS='-Wl,--no-as-needed -lnuma')
    run(['./configure', f'--prefix={work / "install"}'],
        cwd=src, env=env, log=logs / 'configure.log')
    run(['make', f'-j{jobs}'], cwd=src, log=logs / 'build.log')
    run(['make', 'install'], cwd=src, log=logs / 'install.log')

    say('\n[5/6] Create workload helpers')
    (work / 'run').write_text(
        '#!/bin/sh\nset -eu\n' +
        f'export OCAML_FAR_NODE={far}\n' +
        f'exec numactl --physcpubind={cpu_list} --membind={near} "$@"\n')
    (work / 'run').chmod(0o755)
    say('\n[6/6] Apply NUMA settings')
    activate(work)


def interrupted(signum, frame):
    raise InterruptedError(f'Received signal {signum}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--restore', type=Path, metavar='WORKSPACE')
    actions.add_argument('--activate', type=Path, metavar='WORKSPACE')
    args = parser.parse_args()
    if sys.platform != 'linux' or not shutil.which('apt-get'):
        raise RuntimeError('Run this script on an Ubuntu/Debian Linux node with Python 3.')
    ACTIVE.parent.mkdir(parents=True, exist_ok=True)
    with ACTIVE.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run(['sudo', '-n', 'true'])
        signal.signal(signal.SIGTERM, interrupted)
        if args.restore:
            restore(args.restore.expanduser().resolve())
        elif args.activate:
            activate(args.activate.expanduser().resolve())
        else:
            prepare()


if __name__ == '__main__':
    started = time.monotonic()
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, KeyboardInterrupt, EOFError) as error:
        say(f'\nFAILED: {str(error) or "Interrupted"}')
        sys.exit(1)
    finally:
        say(f'Elapsed: {time.monotonic() - started:.1f} seconds')
