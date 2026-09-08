#!/usr/bin/env python3
"""Log sanitizasyon aracı — python_scripts/sanitize.py (CONVENTIONS.md K1-K8 uyumlu).

Yaptıkları (satır satır, streaming):
  1. Türkçe karakterleri ASCII karşılıklarına çevirir (ö->o, Ç->C, ı->i, ...).
     Diğer aksanlı Latin harfleri de (é->e gibi) standart kütüphane yoluyla
     (unicodedata NFKD) indirger — bilgi kaybını önlemek için.
  2. a-z A-Z 0-9 ve boşluk dışındaki TÜM karakterleri BOŞLUĞA ÇEVİRİR
     (org.apache.hadoop -> org apache hadoop; Bar.java:42 -> Bar java 42).
     Alt çizgi de boşluğa döndüğünden _SOL_/_EOL_ içerikte asla oluşamaz.
  3. Çoklu boşlukları tek boşluğa indirger.
  4. Her satırı "_SOL_ <içerik> _EOL_" biçiminde işaretler ve tüm dosyayı
     TEK bir satıra stitch eder. (Underscore içerikte geçemediğinden
     _SOL_/_EOL_ yalnızca bu script'ten gelir — logtaki "SOL" gibi
     doğal kelimelerle asla çakışmaz.)

Örnek:
  python3 sanitize.py --input ornek.log --output ornek.clean.log --debug
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone

# --- dönüşüm kuralları ------------------------------------------------------

# Türkçe alfabenin ASCII karşılıkları (NFKD'nin tek başına çözemediği "ı" dahil)
TURKISH_MAP = str.maketrans({
    "ç": "c", "Ç": "C",
    "ğ": "g", "Ğ": "G",
    "ı": "i", "İ": "I",
    "ö": "o", "Ö": "O",
    "ş": "s", "Ş": "S",
    "ü": "u", "Ü": "U",
})

KEEP_RE = re.compile(r"[^a-zA-Z0-9 ]+")  # kalamayanlar: harf/rakam/boşluk dışındakiler
SPACE_RE = re.compile(r" +")             # çoklu boşluk -> tek boşluk

# Satır sınırlayıcılar. Underscore filtre tarafından BOŞLUĞA çevrildiğinden
# içerikte asla geçemez; _SOL_/_EOL_ kelimelerini yalnızca bu script yazabilir.
MARKER_SOL = "_SOL_"
MARKER_EOL = "_EOL_"

log = logging.getLogger("sanitize")


def sanitize_line(raw: str) -> str:
    """Bir ham satırı temizler. Bilerek exception fırlatmaz; çağıran taraf yine de korumalı."""
    s = raw.rstrip("\r\n")
    if not s.isascii():
        s = s.translate(TURKISH_MAP)
        # kalan aksanlar için standart çözüm: NFKD ayrıştırma + ASCII dışını at
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = KEEP_RE.sub(" ", s)
    s = SPACE_RE.sub(" ", s).strip()
    return s


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_report(path: str, payload: dict) -> None:
    """report.json'u atomik yazar: geçici dosyaya yaz + os.replace (K8)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    payload = {
        "script": "sanitize.py",
        "status": "running",
        "input": os.path.abspath(args.input),
        "output": os.path.abspath(args.output),
        "started_at_utc": _utcnow(),
        "updated_at_utc": _utcnow(),
        "elapsed_sec": 0.0,
        "lines_read": 0,
        "lines_empty": 0,
        "line_errors": 0,
        "throughput_lines_per_sec": 0.0,
        "report_every": args.report_every,
    }

    def flush(status: str) -> None:
        elapsed = time.monotonic() - started
        payload["status"] = status
        payload["updated_at_utc"] = _utcnow()
        payload["elapsed_sec"] = round(elapsed, 3)
        if elapsed > 0:
            payload["throughput_lines_per_sec"] = round(payload["lines_read"] / elapsed, 1)
        try:
            write_report(args.report, payload)
        except OSError as exc:
            log.warning("report.json yazılamadı: %s", exc)

    try:
        out_dir = os.path.dirname(os.path.abspath(args.output))
        os.makedirs(out_dir, exist_ok=True)

        with open(args.input, "r", encoding="utf-8", errors="replace") as fin, \
             open(args.output, "w", encoding="utf-8", errors="replace") as fout:
            first = True
            debug_samples = 3
            for lineno, raw in enumerate(fin, 1):
                try:
                    content = sanitize_line(raw)
                except Exception as exc:  # tek bir satır tüm koşuyu düşürmesin
                    payload["line_errors"] += 1
                    log.debug("satır %d işlenemedi (%s) — _SOL_ _EOL_ olarak yazıldı", lineno, exc)
                    content = ""
                if not content:
                    payload["lines_empty"] += 1
                if args.debug and debug_samples > 0:
                    debug_samples -= 1
                    log.debug("örnek dönüşüm: %r -> %r", raw[:80], content[:80])
                if not first:
                    fout.write(" ")
                fout.write(("%s %s %s" % (MARKER_SOL, content, MARKER_EOL)) if content
                           else "%s %s" % (MARKER_SOL, MARKER_EOL))
                first = False
                payload["lines_read"] = lineno
                if lineno % args.report_every == 0:  # K8: çalışırken sık güncelle
                    flush("running")
                    log.info("%d satır işlendi (%.1f satır/sn)",
                             lineno, payload["throughput_lines_per_sec"])
            fout.write("\n")

        payload["output_bytes"] = os.path.getsize(args.output)
        flush("completed")
        log.info("bitti: %d satır, %d boş, %d satır hatası, %.1f sn, çıktı %d bayt",
                 payload["lines_read"], payload["lines_empty"], payload["line_errors"],
                 payload["elapsed_sec"], payload["output_bytes"])
        return 0
    except KeyboardInterrupt:
        flush("interrupted")
        log.error("kullanıcı tarafından kesildi (kısmi çıktı bırakıldı)")
        return 130
    except Exception as exc:  # traceback yok, temiz mesaj, sıfır dışı çıkış (K3)
        payload["error"] = "%s: %s" % (type(exc).__name__, exc)
        flush("failed")
        log.error("hata: %s", payload["error"])
        return 1


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="sanitize.py",
        description="Log sanitizasyonu: Türkçe->ASCII, a-zA-Z0-9+boşluk dışını boşluğa "
                    "çevirme, çoklu boşluk ünlemi, satırları _SOL_/_EOL_ ile tek satıra stitch.")
    p.add_argument("--input", required=True, help="girdi log dosyası")
    p.add_argument("--output", required=True, help="çıktı dosyası (tek satırlık)")
    p.add_argument("--report", default="report.json",
                   help="çalışma raporu dosyası (varsayılan: report.json)")
    p.add_argument("--report-every", type=int, default=50000,
                   help="report.json güncelleme aralığı, satır sayısı (varsayılan: 50000)")
    p.add_argument("--debug", action="store_true", help="ayrıntılı log (stderr)")
    return p.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,  # K4: loglar stderr'de, stdout temiz
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    sys.exit(run(args))


if __name__ == "__main__":
    main()
