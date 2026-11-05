# FFT benchmark on Linux

Put `setup.py`, `collect.py`, and this README together in a directory on the
Linux machine. Open a Bash terminal in that directory and run the command blocks
below in order. Stay in that directory throughout these instructions.

Use an Ubuntu/Debian machine with at least two NUMA nodes containing memory,
Python 3, and passwordless sudo. No opam or Dune is needed.

## 1. Prepare OCaml and NUMA

```bash
python3 setup.py
```

Example setup choices for a machine with NUMA nodes 0 and 1:

| Prompt | Selection |
| --- | --- |
| OCaml repository | `https://github.com/HExSA-Lab/ocaml.git` |
| Branch | `STRATA-5.5.0` |
| Workspace | Accept the suggested new directory, or choose a new one |
| Far NUMA node | `1` |
| Near NUMA node | `0` |
| Experiment type | `1` (single CPU) |
| Workload CPU | An available CPU on node 0; normally `0` |
| Offline far-node CPUs | `Y` |

Other node IDs work too; choose them from the topology displayed by setup.
FFT is sequential, so select one available CPU on the chosen near node.

Setup displays the topology before these choices. It checks CPU membership,
installs missing dependencies, builds OCaml, disables automatic NUMA balancing,
and applies the selected settings. It does not lock CPU frequency.

Wait for READY, then load the environment:

```bash
source "$(cat "$HOME/.cache/ocaml-numa-setup.active")/env.sh"
```

This defines `WORKSPACE` as the actual directory created by setup and selects
its installed compiler. `$WORKSPACE/run` launches workloads with the selected
CPU and memory settings. Setup has not downloaded or built FFT yet.

## 2. Download and compile FFT

```bash
git clone https://github.com/plabondutta/ocaml_fft.git "$WORKSPACE/ocaml_fft"

ocamlopt -unsafe -inline 100 -I +unix unix.cmxa \
  "$WORKSPACE/ocaml_fft/bin/ft.ml" -cclib -lnuma \
  -o "$WORKSPACE/ocaml_fft/ft.native"
```

This fork contains the `FT_FAR` placement selector and the solver loop timer.
The executable links to the runtime from the OCaml installation selected above.

## 3. Choose which arrays are far

`FT_FAR` is an environment variable read by FFT when it starts. Each set bit
selects one array group for far allocation; the other groups stay near.
The physical near and far nodes come from setup, not from the mask number.

| Value to add | Group placed far |
| ---: | --- |
| 1 | `u0` |
| 2 | `u1` |
| 4 | `u2` |
| 8 | `indexmap` |
| 16 | `ex` |

**All integers from 0 through 31 are supported**, giving 32 placement
combinations. Add the values for the groups you want far. These are examples;
every group not selected stays near:

- `0`: all five groups near.
- `3 = 1 + 2`: `u0` and `u1` far.
- `5 = 1 + 4`: `u0` and `u2` far.
- `6 = 2 + 4`: `u1` and `u2` far.
- `7 = 1 + 2 + 4`: all three grids far; `indexmap` and `ex` near.
- `9 = 1 + 8`: `u0` and `indexmap` far.
- `18 = 2 + 16`: `u1` and `ex` far.
- `24 = 8 + 16`: `indexmap` and `ex` far; all three grids near.
- `31 = 1 + 2 + 4 + 8 + 16`: all five groups far.

Each grid (`u0`, `u1`, `u2`) contains a real array and an imaginary array;
its bit controls both. Small temporary arrays retain ordinary allocation.
The mask selects groups, not a percentage of total memory.

For a single manual Class B run with `u0` and `u2` far:

```bash
FT_FAR=5 "$WORKSPACE/run" "$WORKSPACE/ocaml_fft/ft.native" B
```

This prints results to the terminal; it does not create a CSV. Skip this
manual run if you only want the automated measurements below.

## 4. Run the automated benchmark

Create a fresh results directory and copy the shared runner into it:

```bash
RESULTS="$WORKSPACE/ocaml_fft/results/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RESULTS/logs"
cp collect.py "$RESULTS/collect.py"
```

Keep this directory structure: the runner locates the FFT executable and
workspace relative to its own location.

By default, `collect.py` automatically runs masks `0, 1, 2, 4, 8, 16, 31`.
You can choose any set of masks by editing `MASKS` in the copied
`$RESULTS/collect.py`. For example, this list also includes grid combinations:

```python
MASKS = [0, 1, 2, 4, 8, 16, 3, 5, 6, 7, 31]
```

Other masks such as `9`, `18`, or `24` can be added to the list as well.
To benchmark every possible placement, use:

```python
MASKS = list(range(32))
```

Include mask `0` if you want an all-near baseline for slowdown calculations.

Then run this block once. It runs Class A and Class B sequentially and restores
the CPU and NUMA settings when the block finishes or a benchmark fails:

```bash
(
  set -e
  trap 'python3 "$WORKSPACE/setup.py" --restore "$WORKSPACE"' EXIT
  for CLASS in A B; do
    python3 "$RESULTS/collect.py" "$CLASS"
  done
)
```

For Class B only, change `for CLASS in A B` to `for CLASS in B`.
Keep the terminal connected while it runs.

For each class, the runner handles everything automatically:

1. Run one all-near warm-up process, excluded from the CSV.
2. Run five rounds, with each selected mask once per round and the starting
   mask rotated. Each measurement uses a fresh process; processes run one at a time.
3. Check numerical verification and save each measured process's timing.

The number of measured runs is **five times the number of selected masks**
per class: 35 for seven masks, 55 for eleven, or 160 for all 32. There is also
one warm-up per class. The runner sets `FT_FAR` for each process; you do not
need to change it manually between runs.

One Class A measurement contains six solver iterations over a
256 x 256 x 128 grid. One Class B measurement contains twenty iterations over
a 512 x 256 x 256 grid.

## 5. Find and interpret the results

```bash
echo "$RESULTS"
```

- `measurements.csv`: one row per measured process, including class, mask,
  repetition, timing, and numerical verification.
- `logs/`: full output from each process, including warm-ups.

Use **`loop_wall_s`** for performance comparisons. It times the solver loop:
updates, inverse FFTs, and checksum calculations. Initial allocation,
initialization, the first forward FFT, verification, and printing are outside
this interval. `process_wall_s` instead includes the whole program execution.

Average the five `loop_wall_s` values separately for each class and mask.
For each class, compare with its mask-0 mean:

```text
slowdown (%) = 100 * (placement mean / all-near mean - 1)
```

The CSV contains raw measurements; this runner does not generate figures.
Numerical verification checks the computation. This runner does not query the
physical NUMA location of array pages; placement checks are a separate step.

The automated block above restores the settings. If you stop after setup or a
manual run instead, restore them explicitly:

```bash
python3 "$WORKSPACE/setup.py" --restore "$WORKSPACE"
```

For another experiment with the same installation, source the workspace's
`env.sh` using the path printed by setup, then reactivate it:

```bash
python3 "$WORKSPACE/setup.py" --activate "$WORKSPACE"
```

Create a fresh results directory for each new campaign so previous logs and
measurements are kept.
