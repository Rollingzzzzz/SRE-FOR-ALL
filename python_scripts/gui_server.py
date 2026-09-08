#!/usr/bin/env python3
"""GUI sunucusu — python_scripts/gui_server.py (stdlib; FastAPI gibi dış bağımlılık YOK).

Ne yapar:
  - Tek sayfalık dashboard'u (dashboard.html) ve JSON API'yi servis eder.
  - Pipeline koşularını SUBPROCESS olarak başlatır/durdurur (sunucu asla bloklanmaz,
    koşu çökse bile GUI yaşamaya devam eder — "hiç çökmeyecek" garantisi:
    her uç try/except, eksik/bozuk dosya -> null, yarım dosya riski yok çünkü
    tüm raporlar K8 gereği atomik yazılır).
  - /workspace içinde klasör gezintisi + dışarıdan log upload (akış halinde).

Uçlar:
  GET  /                     -> dashboard.html
  GET  /api/state            -> aktif koşu durumu (pipeline_report + kalp atışı)
  GET  /api/patterns         -> aktif/son koşunun birleşik kalıp tablosu
  GET  /api/log              -> koşunun canlı konsol kuyruğu (son N satır)
  GET  /api/fs?path=...      -> /workspace altında dizin listesi
  GET  /api/runs             -> data/pipeline_* koşu arşivi (gezinti için)
  POST /api/start            -> pipeline.py subprocess başlat (JSON gövde)
  POST /api/stop             -> koşuyu durdur (SIGINT; kısmi çıktılar korunur)
  POST /api/upload?name=...  -> ham gövdeyi /workspace/data/uploads/<name>'a akıt

Çalıştırma (konteyner içinde):
  python3 /workspace/python_scripts/gui_server.py --port 8080
Tarayıcıdan (Windows'tan SSH tüneliyle):
  ssh -L 8080:localhost:8080 sre-dev   ->   http://localhost:8080
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("gui_server")

WORKSPACE = "/workspace"
PIPELINE = os.path.join(WORKSPACE, "python_scripts", "pipeline.py")
DASHBOARD = os.path.join(WORKSPACE, "python_scripts", "dashboard.html")
UPLOAD_DIR = os.path.join(WORKSPACE, "data", "uploads")
DATA_DIR = os.path.join(WORKSPACE, "data")

_proc_lock = threading.Lock()
_proc = {"handle": None, "workdir": "", "log_path": "", "input": "", "pid": None}


def _utcnow() -> int:
    return int(time.time())


def _safe_ws(path: str) -> str:
    """Yolu /workspace'e hapset; kaçış girişimi olursa köke sabitle."""
    real = os.path.realpath(path)
    ws = os.path.realpath(WORKSPACE)
    if not (real == ws or real.startswith(ws + os.sep)):
        return ws
    return real


def read_json(path: str, max_age_sec: float = None):
    """Bozuk/eksik/yaşlı dosyayı sessizce yok sayar — GUI asla çökmez."""
    try:
        if max_age_sec is not None:
            age = time.time() - os.path.getmtime(path)
            if age > max_age_sec:
                return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def current_workdir() -> str:
    """Aktif koşu varsa onun workdir'i; yoksa en yeni data/pipeline_* klasörü."""
    if _proc["workdir"] and proc_alive():
        return _proc["workdir"]
    if _proc["workdir"] and os.path.isfile(os.path.join(_proc["workdir"], "pipeline_report.json")):
        return _proc["workdir"]
    candidates = []
    try:
        for name in os.listdir(DATA_DIR):
            if name.startswith("pipeline_"):
                full = os.path.join(DATA_DIR, name)
                if os.path.isdir(full):
                    candidates.append((os.path.getmtime(full), full))
    except OSError:
        pass
    if not candidates:
        return ""
    return max(candidates)[1]


def proc_alive() -> bool:
    h = _proc["handle"]
    if h is None:
        return False
    return h.poll() is None


def find_live_heartbeat(workdir: str):
    """En güncel rauntun llm/live.json kalp atışı (15 sn'den tazeyse)."""
    try:
        rounds = sorted(d for d in os.listdir(workdir) if d.startswith("round_"))
    except OSError:
        return None
    for r in reversed(rounds):
        hb = read_json(os.path.join(workdir, r, "llm", "live.json"), max_age_sec=15)
        if hb:
            return hb
    return None


def tail_file(path: str, n: int = 200):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-n:])
    except OSError:
        return ""


def stage_hint(workdir: str) -> str:
    """Çalışan pipeline'ın hangi adımda olduğunu workdir içeriğinden tahmin eder
    (heartbeat yoksa: sanitize/chunk/llm/clean)."""
    try:
        rounds = sorted(d for d in os.listdir(workdir) if d.startswith("round_"))
    except OSError:
        rounds = []
    real = [r for r in rounds if not r.endswith("_000") or True]
    if not rounds:
        return "sanitize" if not os.path.isfile(os.path.join(workdir, "round_000", "clean.log")) else "chunk"
    last = os.path.join(workdir, rounds[-1])
    if os.path.isfile(os.path.join(last, "summary.json")):
        return "chunk"  # raunt kapandı, sıradaki rauntun dilimi kesiliyor
    if os.path.isfile(os.path.join(last, "patterns.json")):
        return "clean"
    if os.path.isfile(os.path.join(last, "chunk.txt")):
        return "llm"
    return "chunk"


def clean_live(workdir: str):
    """Aktif clean adımının canlı ilerlemesi (report_clean.json'dan)."""
    try:
        rounds = sorted(d for d in os.listdir(workdir) if d.startswith("round_"))
    except OSError:
        return None
    for r in reversed(rounds):
        rep = read_json(os.path.join(workdir, r, "report_clean.json"), max_age_sec=30)
        if rep and rep.get("status") == "running":
            rd = rep.get("segments_read") or 0
            return "%s / ? segment" % f"{rd:,}"
    return None


def _run_detected(wd: str, hb) -> bool:
    """Koşu tespiti: GUI'nin kendi subprocess'u YA DA (elle başlatılmış koşular için)
    taze kalp atışı / 'running' durumlu taze rapor. Uzun LLM düşüncesi sırasında
    rapor bayatlayabilir — kalp atışı onu kapsar (2 sn'de bir yazılır)."""
    if proc_alive():
        return True
    if hb:
        return True
    rp = os.path.join(wd, "pipeline_report.json") if wd else ""
    if rp and os.path.isfile(rp):
        rep = read_json(rp)
        if rep and rep.get("status") == "running":
            try:
                return (time.time() - os.path.getmtime(rp)) < 1200
            except OSError:
                pass
    return False


def build_tree(root: str, rel: str = "", cap=None) -> dict:
    """Workdir ağacını özyinelemeli listeler (GUI canlı dosya paneli için).
    cap: toplam giriş sayısı sınırrı (güvenlik; normal koşuda asla dolmaz)."""
    if cap is None:
        cap = [4000]
    node = {"name": rel.split("/")[-1] if rel else root.split("/")[-1],
            "rel": rel, "dir": True, "size": 0, "mtime": 0, "children": []}
    try:
        entries = sorted(os.scandir(root), key=lambda e: (not e.is_dir(), e.name.lower()))
    except OSError:
        return node
    for e in entries:
        if cap[0] <= 0:
            break
        cap[0] -= 1
        try:
            st = e.stat()
        except OSError:
            continue
        if e.is_dir():
            node["children"].append(build_tree(e.path, (rel + "/" + e.name).lstrip("/"), cap))
        else:
            node["children"].append({"name": e.name, "rel": (rel + "/" + e.name).lstrip("/"),
                                     "dir": False, "size": st.st_size, "mtime": int(st.st_mtime),
                                     "children": []})
    return node


class Handler(BaseHTTPRequestHandler):
    server_version = "SREgui/1.0"

    def log_message(self, fmt, *args):  # access logunu kapat (konsol temiz kalsın)
        pass

    # ---- yardımcılar -------------------------------------------------------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw.decode("utf-8"))
        except (ValueError, OSError):
            return {}

    # ---- GET ---------------------------------------------------------------
    def do_GET(self):
        try:
            import urllib.parse
            path = self.path.split("?", 1)[0]
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else {})
            q_workdir = qs.get("workdir", [""])[0]
            if path in ("/", "/index.html"):
                with open(DASHBOARD, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/state":
                wd = _safe_ws(q_workdir) if q_workdir else current_workdir()
                hb = find_live_heartbeat(wd) if wd else None
                state = {
                    "running": _run_detected(wd, hb),
                    "workdir": wd,
                    "input": _proc["input"] if _proc["input"] else None,
                    "pipeline": read_json(os.path.join(wd, "pipeline_report.json")) if wd else None,
                    "heartbeat": hb,
                    "stage_hint": stage_hint(wd) if wd and _run_detected(wd, hb) else None,
                    "clean_live": clean_live(wd),
                    "server_time": _utcnow(),
                }
                self._json(state)
            elif path == "/api/patterns":
                wd = _safe_ws(q_workdir) if q_workdir else current_workdir()
                merged = {}
                order = []
                try:
                    rounds = sorted(d for d in os.listdir(wd) if d.startswith("round_"))
                except OSError:
                    rounds = []
                for r in rounds:
                    s = read_json(os.path.join(wd, r, "summary.json"))
                    full = {}
                    pj = read_json(os.path.join(wd, r, "patterns.json"))
                    if pj and isinstance(pj.get("patterns"), list):
                        for p in pj["patterns"]:
                            full[p.get("tag")] = p  # LLM'den dönen TAM kayıt (regex/example dahil)
                    if not s:
                        continue
                    for p in s.get("patterns", []):
                        key = (p.get("tag"), p.get("readable"), p.get("label"))
                        if key not in merged:
                            merged[key] = {"tag": p.get("tag"), "readable": p.get("readable"),
                                           "label": p.get("label"), "matched": 0,
                                           "seen_claimed": p.get("seen_claimed"),
                                           "segments": p.get("segments"),
                                           "over_broad": p.get("over_broad"),
                                           "round": r, "full": full.get(p.get("tag"), {})}
                            order.append(key)
                        merged[key]["matched"] += p.get("matched") or 0
                pats = sorted(merged.values(), key=lambda x: -x["matched"])
                self._json({"workdir": wd, "patterns": pats})
            elif path == "/api/tree":
                wd = _safe_ws(q_workdir) if q_workdir else current_workdir()
                if not wd:
                    self._json({"root": "", "tree": None})
                    return
                self._json({"root": wd, "tree": build_tree(wd)})
            elif path == "/api/file":
                # workdir içindeki bir dosyanın O ANKİ içeriği (büyükse son 64 KB kuyruk)
                wd = _safe_ws(q_workdir) if q_workdir else current_workdir()
                rel = qs.get("path", [""])[0]
                if not wd or not rel or "\x00" in rel:
                    self._json({"error": "path parametresi gerekli"}, 400)
                    return
                full = _safe_ws(os.path.join(wd, rel))
                if not os.path.isfile(full):
                    self._json({"error": "dosya yok (veya klasör): %s" % rel}, 404)
                    return
                st = os.stat(full)
                max_bytes = 65536
                with open(full, "rb") as f:
                    if st.st_size > max_bytes:
                        f.seek(-max_bytes, os.SEEK_END)
                        raw = f.read()
                        truncated = True
                    else:
                        raw = f.read()
                        truncated = False
                self._json({"path": rel, "size": st.st_size, "mtime": int(st.st_mtime),
                            "truncated": truncated, "tail_bytes": max_bytes,
                            "content": raw.decode("utf-8", "replace")})
            elif path == "/api/log":
                wd = _safe_ws(q_workdir) if q_workdir else current_workdir()
                lp = _proc["log_path"] if _proc["log_path"] else os.path.join(wd, "console.log")
                self._json({"workdir": wd, "tail": tail_file(lp)})
            elif path == "/api/fs":
                import urllib.parse
                qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
                target = _safe_ws(qs.get("path", [WORKSPACE])[0])
                entries = []
                try:
                    for name in sorted(os.listdir(target)):
                        full = os.path.join(target, name)
                        try:
                            st = os.stat(full)
                            entries.append({"name": name, "dir": os.path.isdir(full),
                                            "size": 0 if os.path.isdir(full) else st.st_size})
                        except OSError:
                            continue
                except OSError as exc:
                    self._json({"path": target, "error": str(exc), "entries": []})
                    return
                entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
                parent = os.path.dirname(target)
                self._json({"path": target,
                            "parent": parent if _safe_ws(parent) != target else None,
                            "entries": entries})
            elif path == "/api/runs":
                runs = []
                try:
                    for name in sorted(os.listdir(DATA_DIR)):
                        if not name.startswith("pipeline_"):
                            continue
                        full = os.path.join(DATA_DIR, name)
                        rep = read_json(os.path.join(full, "pipeline_report.json")) or {}
                        runs.append({"name": name, "workdir": full,
                                     "status": rep.get("status", "?"),
                                     "coverage": rep.get("coverage_pct"),
                                     "rounds": rep.get("rounds_done", 0),
                                     "mtime": int(os.path.getmtime(full))})
                except OSError:
                    pass
                runs.sort(key=lambda r: -r["mtime"])
                self._json({"runs": runs})
            else:
                self._json({"error": "bilinmeyen uç: %s" % path}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # hiçbir istek sunucuyu düşürmez
            log.warning("GET %s hata: %s", self.path, exc)
            try:
                self._json({"error": str(exc)}, 500)
            except Exception:
                pass

    # ---- POST --------------------------------------------------------------
    def do_POST(self):
        try:
            path = self.path.split("?", 1)[0]
            if path == "/api/start":
                self._start()
            elif path == "/api/stop":
                self._stop()
            elif path == "/api/upload":
                self._upload()
            else:
                self._json({"error": "bilinmeyen uç"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            log.warning("POST %s hata: %s", self.path, exc)
            try:
                self._json({"ok": False, "error": str(exc)}, 500)
            except Exception:
                pass

    def _start(self):
        with _proc_lock:
            if proc_alive():
                self._json({"ok": False,
                            "error": "zaten bir koşu var (önce durdurun)",
                            "workdir": _proc["workdir"]})
                return
            body = self._body_json()
            inp = str(body.get("input") or "").strip()
            if not inp:
                self._json({"ok": False, "error": "girdi dosyası seçilmedi"})
                return
            inp = os.path.realpath(os.path.join(WORKSPACE, inp)
                                   if not os.path.isabs(inp) else inp)
            if not os.path.isfile(inp):
                self._json({"ok": False, "error": "dosya yok: %s" % inp})
                return

            workdir = str(body.get("workdir") or "").strip() or os.path.join(
                DATA_DIR, "pipeline_%s" % datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"))
            argv = ["python3", PIPELINE, "--input", inp, "--workdir", workdir]
            try:
                mr = int(body.get("max_rounds") or 3)
                if mr > 0:
                    argv += ["--max-rounds", str(mr)]
                if body.get("tokens"):
                    argv += ["--tokens", str(int(body["tokens"]))]
                if body.get("min_round_gain_pct") is not None:
                    argv += ["--min-round-gain-pct", str(float(body["min_round_gain_pct"]))]
                if body.get("base_url"):  # test/mok amaçlı
                    argv += ["--llm-base-url", str(body["base_url"])]
            except (TypeError, ValueError):
                self._json({"ok": False, "error": "geçersiz parametre"})
                return

            os.makedirs(workdir, exist_ok=True)
            log_path = os.path.join(workdir, "console.log")
            lf = open(log_path, "w", encoding="utf-8")
            try:
                h = subprocess.Popen(argv, cwd=WORKSPACE, stdout=lf, stderr=subprocess.STDOUT)
            except OSError as exc:
                lf.close()
                self._json({"ok": False, "error": "başlatılamadı: %s" % exc})
                return
            _proc.update({"handle": h, "workdir": workdir, "log_path": log_path,
                          "input": inp, "pid": h.pid})
            threading.Thread(target=h.wait, daemon=True).start()
            self._json({"ok": True, "workdir": workdir, "pid": h.pid,
                        "cmd": " ".join(argv)})

    def _stop(self):
        with _proc_lock:
            h = _proc["handle"]
            if not proc_alive():
                # GUI süreci yok ama koşu tespit edilirse: terminalden başlatılmıştır
                wd = current_workdir()
                hb = find_live_heartbeat(wd) if wd else None
                rep = read_json(os.path.join(wd, "pipeline_report.json")) if wd else None
                if _run_detected(wd, hb) and (rep or {}).get("status") == "running":
                    self._json({"ok": False, "error":
                                "koşu terminalden başlatılmış — GUI'den durdurulamaz; "
                                "ssh sre-dev üzerinde 'pkill -INT -f pipeline.py' "
                                "komutuyla durdurun (kısmi çıktılar korunur)"})
                else:
                    self._json({"ok": False, "error": "aktif koşu yok"})
                return
            try:
                os.kill(h.pid, signal.SIGINT)  # 130 yolu: kısmi çıktılar korunur
                for _ in range(50):
                    if h.poll() is not None:
                        break
                    time.sleep(0.1)
                if h.poll() is None:
                    h.terminate()
                self._json({"ok": True, "stopped": True})
            except OSError as exc:
                self._json({"ok": False, "error": str(exc)})

    def _upload(self):
        import urllib.parse
        qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
        name = os.path.basename(str(qs.get("name", ["log_yuklendi.log"])[0])) or "log_yuklendi.log"
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._json({"ok": False, "error": "boş gövde"})
            return
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        dest = os.path.join(UPLOAD_DIR, name)
        written = 0
        try:
            with open(dest, "wb") as f:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
                    written += len(chunk)
        except OSError as exc:
            self._json({"ok": False, "error": str(exc)})
            return
        self._json({"ok": True, "path": dest, "bytes": written})


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="gui_server.py",
                                description="SRE-FOR-ALL canlı dashboard sunucusu (stdlib)")
    p.add_argument("--port", type=int, default=8080, help="dinlenecek port (varsayılan: 8080)")
    p.add_argument("--host", default="127.0.0.1",
                   help="dinlenecek arayüz (varsayılan: 127.0.0.1 — sadece SSH tüneli)")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(stream=sys.stderr,
                        level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    log.info("GUI hazır: http://%s:%d  (dashboard: %s)", args.host, args.port, DASHBOARD)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("kapatıldı")


if __name__ == "__main__":
    main()
