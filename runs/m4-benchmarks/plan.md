# M4 benchmarks: final runs

Written before the runs. Every script in `benchmarks/` except book.py (M1's decision,
measured on the M4 Pro and unchanged since) runs three times at commit e20d5df, on one
host, one script at a time with nothing else running:

```bash
for r in 1 2 3; do
  for s in decode sign ws_clients http_clients gc_pauses hot_path; do
    python benchmarks/$s.py > runs/m4-benchmarks/$s-$r.txt
  done
done
for v in 3.14 3.14t; do  # venvs without the project's research dependencies
  PYTHONPATH=src:benchmarks python$v benchmarks/gc_pauses.py > runs/m4-benchmarks/gc_pauses-$v.txt
done
```

Each output starts with its platform header. docs/EXECUTION.md's tables are the median
of the three runs, read from these files.
