# M4 benchmarks: the re-run after the merge reviews

Written before the run. The merge reviews changed the scripts after e20d5df: P1 times
its book stage's parse and apply apart, headers print the commit, and the tests got
stricter. The scripts are as of 9da4d3f; the run happens at the commit that adds this
note, which changes nothing else, and every output's header prints that commit (it
must not say "-dirty"). Same host and the same command as `plan.md`, with the outputs
as new files in `rerun/`; the earlier outputs stay as they are.

```bash
for r in 1 2 3; do
  for s in decode sign ws_clients http_clients gc_pauses hot_path; do
    python benchmarks/$s.py > runs/m4-benchmarks/rerun/$s-$r.txt
  done
done
for v in 3.14 3.14t; do  # venvs without the project's research dependencies
  PYTHONPATH=src:benchmarks python$v benchmarks/gc_pauses.py > runs/m4-benchmarks/rerun/gc_pauses-$v.txt
done
```

docs/EXECUTION.md's tables are regenerated from `rerun/` (the median of its three runs).
