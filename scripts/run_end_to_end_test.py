#!/usr/bin/env python3
import os, subprocess, sys, time, urllib.request, csv

BASE=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
server=subprocess.Popen([sys.executable,os.path.join(BASE,"mock_api","mock_api.py"),
                         "--host","127.0.0.1","--port","8000"])
try:
    for _ in range(30):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8000/health",timeout=1) as r:
                if r.status==200: break
        except Exception: time.sleep(.2)
    else: raise RuntimeError("Mock API did not start")

    for name in ("orders_get.yaml","orders_post.yaml","orders_deep.yaml"):
        cfg=os.path.join(BASE,"config",name)
        print("\nRunning:",name)
        rc=subprocess.run([sys.executable,os.path.join(BASE,"api_to_csv.py"),
                           "--config",cfg],cwd=BASE).returncode
        if rc: raise SystemExit(f"FAILED: {name}, exit={rc}")

    for name in ("orders_get.csv","orders_post.csv","orders_deep.csv"):
        path=os.path.join(BASE,"output",name)
        with open(path,encoding="utf-8-sig",newline="") as f: rows=list(csv.DictReader(f))
        print(f"PASS: {name}: {len(rows)} rows")
        if not rows: raise SystemExit(f"Empty output: {name}")
    print("\nEND-TO-END TEST PASSED")
finally:
    server.terminate()
    try: server.wait(timeout=5)
    except subprocess.TimeoutExpired: server.kill()
