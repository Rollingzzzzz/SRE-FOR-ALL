#!/usr/bin/env python3
"""Dilim kesme aracı — python_scripts/chunk.py (CONVENTIONS.md K1-K8 uyumlu).

Görev (high-level):
  Elimizdeki dev tek satırlık (_SOL_/_EOL_'lu) log'dan, AI'a göndereceğimiz
  kadar metni keser. Makas satırın ortasında kapanmaz: kesim yeri daima
  bir _EOL_'a kaydırılır — AI'a havada kalan yarım satır gitmez.

PENCERE SEÇİMİ (daîresel rastgele — varsayılan):
  Dilim dosyanın RASTGELE bir segmentinden başlar. Dosya sonuna
  denk gelinirse BAŞA SARILIR ve bant (hedef ±tolerans) dolana dek baştan
  devam edilir — asla 3k'lık cılız dilim dönülmez. Amaç: dilimin kütleye
  oranlı temsil etmesi (dev kalıpların yaşadığı bölgeler de sıraya girer).
  Rasgele nokta segment ortasına düşebilir: okuma ilk '_SOL_' tokenına
  yaslanarak başlar (içerikte '_SOL_' asla geçemediğinden yaslanma tek
  anlamlıdır). Sarma en fazla BİR turdur — küçük dosyada içerik çoğaltılmaz.
  --mode head ile eski (baştan alan) davranış korunur; --start-offset
  belirli bir konumu zorlar (test/tekrarlanabilirlik); --seed rastgeleliği
  sabitler. Seçilen konum ve tohum report.json'a düşer (denetim izi).

Girdi sözleşmesi: girdi MUTLAKA sanitize.py çıktısı olmalıdır (ilk kelime
  _SOL_). Değilse script 'önce sanitize.py çalıştırın' hatası verip çıkar —
  sanitize edilmemiş dosyayı sessizce işlemeye çalışmaz.

Nasıl:
  --tokens N ile istenen token bütçesi verilir (ör. 10000). Script kelimeleri
  akışta sayar; her kelimenin token maliyeti uzunluğundan tahmin edilir
  (varsayılan: 4 karakter ≈ 1 token). Bütçe dolunca kesim en yakın _EOL_
  sınırına kaydırılır; hedef ±tolerans (varsayılan %10) içinde kalmaya çalışılır.

Örnek:
  python3 chunk.py --input ornek.clean.log --output dilim.txt --tokens 10000
  python3 chunk.py --input ornek.clean.log --output dilim.txt --tokens 10000 \
      --mode head            # eski davranış: dosyanın başından
  python3 chunk.py --input ornek.clean.log --output dilim.txt --tokens 10000 \
      --seed 42              # tekrarlanabilir rastgele pencere
"""

import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone

WORD_READ_CHUNK = 1 << 20  # 1 MB'lık bloklarla oku (K2: tümünü belleğe yükleme)

# Satır marker'ları — sanitize.py'nin yazdığı biçimle birebir aynı olmalı.
# Underscore sanitize edilmiş içerikte geçemediğinden bu kelimeleri yalnızca
# sanitize.py yazabilir; içerikteki doğal "SOL"/"EOL" kelimeleriyle asla çakışmaz.
# Girdi sözleşmesi: girdi mutlaka sanitize.py çıktısıdır (ilk kelime _SOL_).
MARKER_SOL = "_SOL_"
MARKER_EOL = "_EOL_"

log = logging.getLogger("chunk")


def first_token(path: str) -> str:
    """Dosyanın İLK kelimesini okur (sözleşme kontrolü için; az veri okur)."""
    with open(path, "rb") as f:
        head = f.read(4096)
    parts = head.decode("utf-8", "replace").split()
    return parts[0] if parts else ""


def _circle_blocks(f, start_offset: int):
    """Bayt blokları: start_offset→EOF, sonra 0→start_offset (en fazla bir tur).

    Sarma anında boş bytes döndürür — kelime katmanı oraya tek bir boşluk
    koyar; dairesel eklem ' _EOL_ _SOL_ ' formatında kalır."""
    f.seek(start_offset)
    while True:
        block = f.read(WORD_READ_CHUNK)
        if not block:
            break
        yield block
    if start_offset > 0:
        yield b""  # sarma işareti
        f.seek(0)
        left = start_offset
        while left > 0:
            block = f.read(min(WORD_READ_CHUNK, left))
            if not block:
                break
            yield block
            left -= len(block)


def iter_words(path: str, start_offset: int = 0, circle: dict = None):
    """Dosyayı blok blok okuyup kelime akışı üretir; bellek kullanımı sabit kalır.

    start_offset=0 → doğrusal akış (eski davranış). start_offset>0 → o konumdan
    EOF'a, ardından (tek tur) baştan start_offset'a. Konum segment ortasına
    düşebileceğinden akış, ilk '_SOL_' tokenı görülene dek kelime ÜRETMEZ
    (kısmi kelime çöpü dilime sızamaz). Sarma olursa circle['wrapped']=True olur.
    """
    tail = ""
    snapped = start_offset == 0  # 0 konumu zaten '_SOL_' ile başlar (sözleşme)
    with open(path, "rb") as f:
        for block in _circle_blocks(f, start_offset):
            if not block:
                # dairesel eklem: kuyrukla baş arasında tam bir boşluk garanti
                if circle is not None:
                    circle["wrapped"] = True
                if tail:
                    tail += " "
                continue
            parts = (tail + block.decode("utf-8", "replace")).split()
            if not parts:
                tail = ""
                continue
            tail = parts.pop()
            for w in parts:
                if not snapped:
                    if w == MARKER_SOL:
                        snapped = True  # segment başına yaslandık — akış başlar
                    else:
                        continue  # rastgele konumun kısmi kelimesi: atla
                yield w
    if tail and snapped:
        yield tail


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
    target = float(args.tokens)
    cpt = float(args.chars_per_token)
    tol = float(args.tolerance)
    lo, hi = target * (1.0 - tol), target * (1.0 + tol)

    payload = {
        "script": "chunk.py",
        "status": "running",
        "input": os.path.abspath(args.input),
        "output": os.path.abspath(args.output),
        "started_at_utc": _utcnow(),
        "updated_at_utc": _utcnow(),
        "elapsed_sec": 0.0,
        "tokens_requested": args.tokens,
        "chars_per_token": cpt,
        "tolerance": tol,
        "tolerance_band": [round(lo, 1), round(hi, 1)],
        "window": {"mode": args.mode, "seed": args.seed,
                   "start_offset_byte": None, "wrapped": False},
        "words_seen": 0,
        "words_selected": 0,
        "lines_selected": 0,
        "tokens_selected": 0.0,
        "cut_reason": "",
        "tolerance_ok": None,
        "report_every": args.report_every,
    }

    def flush(status: str) -> None:
        elapsed = time.monotonic() - started
        payload["status"] = status
        payload["updated_at_utc"] = _utcnow()
        payload["elapsed_sec"] = round(elapsed, 3)
        try:
            write_report(args.report, payload)
        except OSError as exc:
            log.warning("report.json yazılamadı: %s", exc)

    try:
        out_dir = os.path.dirname(os.path.abspath(args.output))
        os.makedirs(out_dir, exist_ok=True)

        # --- pencere konumu seçimi ---------------------------------------
        file_size = os.path.getsize(args.input)
        seed_used = args.seed
        if args.start_offset is not None:
            if not (0 <= args.start_offset < max(file_size, 1)):
                raise ValueError("--start-offset %d dosya boyutuna (%d) uymuyor"
                                 % (args.start_offset, file_size))
            start_offset = args.start_offset
            payload["window"]["mode"] = "offset"
        elif args.mode == "head":
            start_offset = 0
        else:  # random (varsayılan)
            rng = random.Random(seed_used)
            start_offset = rng.randrange(file_size) if file_size > 0 else 0
        payload["window"]["seed"] = seed_used
        payload["window"]["start_offset_byte"] = start_offset
        log.info("pencere: mode=%s start_offset=%d bayt (dosya %d bayt)",
                 payload["window"]["mode"], start_offset, file_size)

        # --- girdi sözleşmesi: DOSYANIN ilk kelimesi _SOL_ olmalı ---------
        # (pencere rastgele bir yerden başladığı için kontrol pencereden değil
        #  dosyanın başından yapılır — sözleşme dosyanın kendisi hakkında)
        fw = first_token(args.input)
        if fw != MARKER_SOL:
            payload["cut_reason"] = "not_sanitized"
            payload["error"] = ("girdi sanitize.py çıktısı gibi görünmüyor "
                                "(ilk kelime %r, beklenen %r) — önce sanitize.py çalıştırın"
                                % (fw, MARKER_SOL))
            flush("failed")
            log.error("hata: %s", payload["error"])
            return 1

        circle = {"wrapped": False}
        est_total = 0.0        # görülen kelimelerin toplam token tahmini
        safe_tokens = 0.0      # son yazılan (EOL ile biten) güvenli kesimin token'ı
        line_buf = []          # son EOL'dan beri biriken kelimeler (tek satır — bellek küçük)
        words_seen = 0
        words_selected = 0
        lines_selected = 0
        first_line = True
        debug_lines = 2
        done_reason = None
        tolerance_ok = True

        with open(args.output, "w", encoding="utf-8", errors="replace") as fout:
            for w in iter_words(args.input, start_offset, circle):
                words_seen += 1
                est_total += (len(w) + 1) / cpt  # +1: ayıran boşluk
                line_buf.append(w)

                if w == MARKER_EOL:
                    line_tokens = est_total
                    if line_tokens > hi:
                        # Bu satır toleransı taşırdı: son güvenli EOL ile bu satır
                        # arasından hedefe daha yakın olanı seç. Hiç satır yazılmadıysa
                        # boş çıktı anlamsız olacağından ilk satır her zaman yazılır.
                        if safe_tokens > 0 and (target - safe_tokens) <= (line_tokens - target):
                            done_reason = "trimmed_to_prev_eol"   # zaten yazılmış kısımda kaldık
                        else:
                            if not first_line:
                                fout.write(" ")
                            fout.write(" ".join(line_buf))
                            words_selected += len(line_buf)
                            lines_selected += 1
                            safe_tokens = line_tokens
                            done_reason = "extended_to_next_eol"
                        if done_reason == "trimmed_to_prev_eol":
                            tolerance_ok = safe_tokens >= lo
                        else:  # extend seçildi: bu satır hi üzerinde, tolerans dışı
                            tolerance_ok = False
                        if not tolerance_ok:
                            log.warning("tolerans aşıldı: hedef %.0f, seçilen %.0f token "
                                        "(bant: %.0f-%.0f)", target, safe_tokens, lo, hi)
                        break
                    # satırı güvenli kesim olarak yaz
                    if not first_line:
                        fout.write(" ")
                    line_text = " ".join(line_buf)
                    fout.write(line_text)
                    if args.debug and debug_lines > 0:
                        debug_lines -= 1
                        log.debug("örnek satır: %s", line_text[:100])
                    words_selected += len(line_buf)
                    lines_selected += 1
                    safe_tokens = est_total
                    first_line = False
                    line_buf = []
                    if est_total >= target:
                        done_reason = "at_target_eol"
                        break
                elif safe_tokens == 0 and len(line_buf) >= args.max_words_no_eol:
                    # EOL hiç gelmedi ve kelime sınırı doldu: eldekini gönder, sonsuz bellek büyümesine izin verme
                    fout.write(" ".join(line_buf))
                    words_selected += len(line_buf)
                    safe_tokens = est_total
                    done_reason = "no_eol_word_cap"
                    tolerance_ok = False
                    log.warning("girdide _EOL_ bulunamadı ve kelime sınırına (%d) ulaşıldı — "
                                "bütçe aşılmiş olabilir", args.max_words_no_eol)
                    line_buf = []
                    first_line = False
                    break

                if words_seen % args.report_every == 0:  # K8: çalışırken güncelle
                    payload["words_seen"] = words_seen
                    payload["words_selected"] = words_selected
                    payload["lines_selected"] = lines_selected
                    payload["tokens_selected"] = round(safe_tokens, 1)
                    flush("running")
                    log.info("%d kelime tarandı, %.0f token birikti", words_seen, est_total)

            else:
                # dairesel akış bitti (bir tam tur) ve hedefe ulaşılamadı
                if line_buf:
                    if lines_selected == 0:
                        # dosya bütçeden küçük VEYA hiç EOL yok: elimizdeki her şeyi gönder
                        fout.write(" ".join(line_buf))
                        words_selected += len(line_buf)
                        safe_tokens = est_total
                        done_reason = "eof_sent_all"
                        tolerance_ok = None  # tolerans kavramı uygulanamaz
                        log.info("girdi bütçeden küçük ya da EOL içermiyor — tüm dosya gönderildi")
                    elif circle.get("wrapped"):
                        # tur başındaki segmentin kuyruğu: çevrimi tamamla (yarım bırakma)
                        fout.write(" " + " ".join(line_buf))
                        words_selected += len(line_buf)
                        safe_tokens = est_total
                        done_reason = "circle_completed"
                        tolerance_ok = None
                        log.info("dairesel tur tamamlandı — ilk segmentin kuyruğu çevrime eklendi")
                    else:
                        # son satır EOL'süz kaldı: yarım satır göndermeyiz, temiz kesimde dur
                        log.warning("son satır _EOL_ ile bitmiyor — kuyrukta kalan %d kelime bırakıldı",
                                    len(line_buf))
                        done_reason = "eof_short_input"
                        tolerance_ok = (safe_tokens >= lo) if safe_tokens > 0 else False
                else:
                    if lines_selected > 0:
                        done_reason = "eof_whole_file"   # dosya bütçeden küçük, eksiksiz alındı
                        tolerance_ok = None
                    else:
                        done_reason = "eof_empty_input"
                        tolerance_ok = True

            fout.write("\n" if words_selected > 0 else "")

        payload["window"]["wrapped"] = circle.get("wrapped", False)
        payload.update({
            "words_seen": words_seen,
            "words_selected": words_selected,
            "lines_selected": lines_selected,
            "tokens_selected": round(safe_tokens, 1),
            "cut_reason": done_reason,
            "tolerance_ok": tolerance_ok,
            "output_bytes": os.path.getsize(args.output),
        })
        flush("completed")
        log.info("bitti: %d kelime / %d satır seçildi, ~%.0f token (hedef %d, bant %.0f-%.0f), "
                 "kesim: %s, pencere: %s@%d%s, süre %.2f sn",
                 words_selected, lines_selected, safe_tokens, args.tokens, lo, hi,
                 done_reason, payload["window"]["mode"], start_offset,
                 " (sarıldı)" if circle.get("wrapped") else "",
                 payload["elapsed_sec"])
        return 0
    except KeyboardInterrupt:
        flush("interrupted")
        log.error("kullanıcı tarafından kesildi")
        return 130
    except Exception as exc:  # traceback yok, temiz mesaj, sıfır dışı çıkış (K3)
        payload["error"] = "%s: %s" % (type(exc).__name__, exc)
        flush("failed")
        log.error("hata: %s", payload["error"])
        return 1


def positive_int(value: str) -> int:
    try:
        iv = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("tam sayı girin: %r" % value)
    if iv <= 0:
        raise argparse.ArgumentTypeError("sıfırdan büyük olmalı: %r" % value)
    return iv


def nonnegative_int(value: str) -> int:
    try:
        iv = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("tam sayı girin: %r" % value)
    if iv < 0:
        raise argparse.ArgumentTypeError("negatif olamaz: %r" % value)
    return iv


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="chunk.py",
        description="AI'a gönderilecek token bütçeli metin dilimi keser. Pencere "
                    "varsayılan olarak RASTGELE bir segmentten başlar; dosya sonuna "
                    "denk gelinirse başa sarılıp bant dolana dek tamamlanır (dairesel, "
                    "en fazla bir tur). Girdi MUTLAKA sanitize.py çıktısı olmalı "
                    "(ilk kelime _SOL_). Kesim daima _EOL_ sınırında biter.")
    p.add_argument("--input", required=True, help="girdi dosyası (sanitize çıktısı önerilir)")
    p.add_argument("--output", required=True, help="çıktı dosyası (seçilen dilim)")
    p.add_argument("--tokens", required=True, type=positive_int,
                   help="istenen token bütçesi (ör. 10000)")
    p.add_argument("--mode", choices=["random", "head"], default="random",
                   help="pencere konumu: random (varsayılan) | head (eski davranış)")
    p.add_argument("--seed", type=nonnegative_int, default=None,
                   help="rastgelelik tohumu (verilirse koşu tekrarlanabilir; rapora düşer)")
    p.add_argument("--start-offset", type=nonnegative_int, default=None,
                   help="pencere konumunu bayt cinsinden zorla (test/tekrarlanabilirlik; "
                        "mode'u geçersiz kılar)")
    p.add_argument("--chars-per-token", type=float, default=4.0,
                   help="token tahmini oranı, karakter/token (varsayılan: 4.0)")
    p.add_argument("--tolerance", type=float, default=0.10,
                   help="kesim toleransı, oran (varsayılan: 0.10 = ±%%10)")
    p.add_argument("--report", default="report.json",
                   help="çalışma raporu dosyası (varsayılan: report.json)")
    p.add_argument("--report-every", type=int, default=50000,
                   help="report.json güncelleme aralığı, taranan kelime sayısı (varsayılan: 50000)")
    p.add_argument("--max-words-no-eol", type=int, default=5000000,
                   help="bozuk girdide (_SOL_ var ama _EOL_ hiç yok) belleği korumak için "
                        "gönderilecek en fazla kelime (varsayılan: 5000000)")
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
