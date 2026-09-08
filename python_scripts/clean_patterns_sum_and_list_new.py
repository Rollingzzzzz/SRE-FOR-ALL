#!/usr/bin/env python3
"""Öğrenilmiş kalıpları temizle + readable sıklık özeti + yenileri listele.

python_scripts/clean_patterns_sum_and_list_new.py (CONVENTIONS.md K1-K8 uyumlu)

Üç iş bir arada (adındaki üç fiil):
  1) clean_patterns — LLM'in öğrendiği kalıp (regex) kurallarıyla eşleşen
     segmentleri/olayları girdiden ELER. Çoklu-segment kurallar (regex'inde
     " _EOL_ _SOL_ " ayracı olanlar) olayı ATOMİK eler: 40 satırlık stack
     trace'in 25'i eşleşip 15'i eşleşmeyorsa HİÇBİRİ elenmez.
  2) sum — elenen her kalıbın READABLE halini ve kaç kez görüldüğünü bir JSON
     özet dosyasına yazar (mühendis gözünden sıklık raporu: "×14 _SOL_
     _TIMESTAMP_ ... Spilling map output _EOL_ ...").
  3) list_new — hiçbir kurala eşleşmeyen segmentleri AYNI sanitize formatında
     (_SOL_ ... _EOL_ tek satır) yeni dosyaya bırakır; bu dosya chunk.py'a
     beslenir -> sonraki LLM turu (öğrenme döngüsü burada kapanır).

Girdi sözleşmeleri:
  --input : sanitize.py çıktısı (segmentler _SOL_ ile başlar), aksi halde
            "önce sanitize.py çalıştırın" hatası + rc=1.
  --rules : call_llm_for_pattern_recon.py çıktısı ({"patterns":[...]});
            BİRDEN ÇOK verilebilir (birikimli baseline). Her kural compile +
            self-match (kendi example'ına fullmatch) kapısından geçer;
            geçmeyen skip edilir + özete işlenir, koşu asla düşmez.
            Geçerli kural kalmazsa rc=1.

Motor (streaming, sabit bellek):
  Segmentler blok blok okunup "_EOL_" tokenından bölünür (underscore
  invariant: içerikte _EOL_ asla oluşamaz -> her bölme gerçek sınırdır; blok
  kenarında yarım kalan marker sonraki bloğa yapıştırılır). Kurallar uzun->
  kısa sıralı, ilk eşleşme kazanır (attribution: özgün kural krediyi alır).
  Kuralın erişimi (kaç segmente uzanabildiği) regex'indeki _EOL_ sayısından
  hesaplanır; (...)+ gibi sınırsız tekrarlar --max-event-lines ile sınırlanır.
  Bellek = en uzun kuralın erişimi kadardır.

Koruma denklemi (her koşuda kanıtlanır, K-ruhuna uygun):
  segments_in == segments_removed + segments_kept
  Bozulursa (tek segment kaybolursa) SERT hata + rc=1 — sessiz kayıp imkânsız.

Örnek:
  python3 python_scripts/clean_patterns_sum_and_list_new.py \
    --input data/hadoop.clean.log \
    --rules data/llm/experiment_multiline_patterns_v3.json \
    --output data/unmatched_round1.log \
    --summary data/summary_round1.json \
    --report data/report_clean_round1.json
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone

log = logging.getLogger("clean_patterns")

READ_CHUNK = 1 << 20          # 1 MB bloklarla oku (K2)
MARKER_EOL = "_EOL_"
MARKER_SOL = "_SOL_"
ANCHOR_RE = re.compile(r"[A-Za-z0-9_ ]+")          # saf literal koşuları
ESCAPE_RE = re.compile(r"\\[A-Za-z.]")             # \d \w \s \. ikilileri
UNBOUNDED_RE = re.compile(r"\)[+*]")               # (...)+ veya (...)* sınırsız tekrar


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_report(path: str, payload: dict) -> None:
    """report.json'u atomik yazar: geçici dosyaya yaz + os.replace (K8)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def iter_segments(path: str):
    """Dosyayı blok blok okuyup segment akışı üretir: "_SOL_ icerik _EOL_".
    İçerikte _EOL_ oluşamayacağından her bölme gerçek segment sonudur; blok
    kenarında yarım kalan marker ('_EO' + 'L_') sonraki bloğa yapıştırılır."""
    tail = ""
    warned_tail = False
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        while True:
            block = f.read(READ_CHUNK)
            if not block:
                break
            buf = tail + block
            pieces = buf.split(MARKER_EOL)
            tail = pieces.pop()          # son parça tamamlanmamış olabilir
            for piece in pieces:
                seg = piece.strip()
                if seg:
                    yield seg + " " + MARKER_EOL   # kural uzayında segment marker'ıyla tamdır
    leftover = tail.strip()
    if leftover:
        # sanitize çıktısında olmaması gereken durum: _EOL_'süz kuyruk
        if not warned_tail:
            log.warning("girdi sonu _EOL_ içermeyen kuyrukla bitiyor — "
                        "olduğu gibi survivor'a düşecek")
            warned_tail = True
        yield leftover


def longest_anchor(regex_src: str) -> str:
    r"""Regex'teki en uzun saf-literal kelime koşusu (prefilter için).
    \d gibi kaçışlar temizlenir; metakarakterler koşuyu böler. Boş dönebilir
    (o durumda prefilter yok, regex her seferinde koşar)."""
    cleaned = ESCAPE_RE.sub(" ", regex_src)
    best = ""
    for run in ANCHOR_RE.findall(cleaned):
        r = run.strip()
        if len(r) > len(best):
            best = r
    return best


def load_rules(paths, only_tags, exclude_tags, max_event_lines: int):
    """Kural dosyalarını yükler + doğrular. (rules, skipped) döner.
    Doğrulama kapıları: compile, self-match (example'a fullmatch), yinelenen
    regex. Geçmeyen kural skip edilir — koşu düşmez."""
    rules = []
    skipped = []
    seen_regex = set()
    n_files = 0
    for path in paths:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        pats = data.get("patterns") if isinstance(data, dict) else None
        if not isinstance(pats, list) or not pats:
            raise ValueError("kural dosyasında patterns[] yok/boş: %s" % path)
        n_files += 1
        for p in pats:
            tag = str(p.get("tag") or "?")
            if only_tags and tag not in only_tags:
                skipped.append({"tag": tag, "reason": "filtre (--only-rules dışı)"})
                continue
            if exclude_tags and tag in exclude_tags:
                skipped.append({"tag": tag, "reason": "filtre (--exclude-rules)"})
                continue
            regex_src = p.get("regex") or ""
            if not regex_src:
                skipped.append({"tag": tag, "reason": "regex yok/boş"})
                continue
            try:
                rx = re.compile(regex_src)
            except re.error as exc:
                skipped.append({"tag": tag, "reason": "compile hatası: %s" % exc})
                continue
            example = p.get("example") or ""
            if example:
                norm = " ".join(example.split())
                if rx.fullmatch(norm) is None:
                    skipped.append({"tag": tag, "reason": "self-match başarısız "
                                                          "(regex kendi örneğine denk düşmüyor)"})
                    continue
            if regex_src in seen_regex:
                skipped.append({"tag": tag, "reason": "yinelenen regex (ilk hali kullanıldı)"})
                continue
            seen_regex.add(regex_src)
            unbounded = bool(UNBOUNDED_RE.search(regex_src))
            span = max_event_lines if unbounded else max(1, regex_src.count(MARKER_EOL))
            rules.append({
                "tag": tag,
                "label": str(p.get("label") or ""),
                "readable": str(p.get("readable") or ""),
                "regex": regex_src,
                "rx": rx,
                "anchor": longest_anchor(regex_src),
                # sınırsız kurallar için hızlı kapı: BAŞLIK kısmındaki en uzun literal
                # (ilk '(' öncesi) — bu yoksa kural buradan başlayamaz, match bile koşmaz
                "head_anchor": (longest_anchor(regex_src.split("(")[0])
                                or longest_anchor(regex_src)),
                "span": span,
                "unbounded": unbounded,
                "seen_claimed": p.get("seen"),
                "count": 0,
            })
    rules.sort(key=lambda r: (-len(r["regex"]), r["tag"]))  # uzun -> kısa
    log.info("kurallar: %d dosya, %d geçerli, %d skip", n_files, len(rules), len(skipped))
    return rules, skipped


def run(args: argparse.Namespace) -> int:
    started = time.monotonic()

    payload = {
        "script": "clean_patterns_sum_and_list_new.py",
        "status": "running",
        "input": os.path.abspath(args.input),
        "output": os.path.abspath(args.output),
        "rules_files": [os.path.abspath(p) for p in args.rules],
        "started_at_utc": _utcnow(),
        "updated_at_utc": _utcnow(),
        "elapsed_sec": 0.0,
        "segments_read": 0,
        "segments_removed": 0,
        "segments_kept": 0,
        "rules_used": 0,
        "report_every": args.report_every,
    }

    def flush(status: str) -> None:
        payload["status"] = status
        payload["updated_at_utc"] = _utcnow()
        payload["elapsed_sec"] = round(time.monotonic() - started, 3)
        try:
            write_report(args.report, payload)
        except OSError as exc:
            log.warning("report.json yazılamadı: %s", exc)

    try:
        only_tags = set(t.strip() for t in (args.only_rules or "").split(",") if t.strip())
        exclude_tags = set(t.strip() for t in (args.exclude_rules or "").split(",") if t.strip())
        rules, skipped = load_rules(args.rules, only_tags, exclude_tags,
                                    args.max_event_lines)
        payload["rules_used"] = len(rules)
        if not rules:
            raise ValueError("geçerli kural kalmadı — kuralları (veya filtreleri) gözden geçirin")
        max_span = max(r["span"] for r in rules)
        log.debug("en uzun kural erişimi: %d segment", max_span)

        seg_iter = iter_segments(args.input)
        try:
            first_seg = next(seg_iter)
        except StopIteration:
            raise ValueError("girdi boş: %s" % args.input)
        if not first_seg.startswith(MARKER_SOL):
            raise ValueError("girdi sanitize.py çıktısı gibi görünmüyor (ilk segment "
                             "%r ile başlıyor, beklenen %r) — önce sanitize.py çalıştırın"
                             % (first_seg[:20], MARKER_SOL))

        out_dir = os.path.dirname(os.path.abspath(args.output))
        os.makedirs(out_dir, exist_ok=True)

        # --- eşleştirme motoru: akışta ele, akışta yaz (K7) -----------------
        pending = []          # karar bekleyen segmentler (<= max_span)
        pending_text = ""     # " ".join(pending) — artımlı tutulur (tekrar join yok)

        def try_head():
            """pending'in başından eşleşme arar. (kural, kaç_segment) döner.
            Sınırlı kurallar: tam-span önek fullmatch (literal-anchor elemeden sonra).
            Sınırsız ((...)+ ) kurallar: başlık-çapası eleme + TEK greedy match —
            önek uzunluklarını tek tek denemek O(n²) olurdu."""
            for r in rules:
                s = r["span"]
                if r["unbounded"]:
                    ha = r["head_anchor"]
                    if ha and ha not in pending_text[:8192]:
                        continue  # başlık çapası yok: bu kural buradan başlayamaz
                    m = r["rx"].match(pending_text)
                    if not m or not m.group(0).endswith(MARKER_EOL):
                        continue
                    if m.end() >= len(pending_text) and len(pending) < max_span:
                        continue  # eşleşme tüm pending'i yuttu: olay büyüyor olabilir, bekle
                    acc = 0
                    for i, seg in enumerate(pending):
                        acc += len(seg) + (1 if i else 0)
                        if acc == m.end():
                            return r, i + 1
                        if acc > m.end():
                            break  # segment sınırına denk gelmedi: eşleşme sayılmaz
                    continue
                if len(pending) < s:
                    continue
                cand = pending[0] if s == 1 else " ".join(pending[:s])
                if r["anchor"] and r["anchor"] not in cand:
                    continue
                if r["rx"].fullmatch(cand):
                    return r, s
            return None, 0

        segments_in = 0
        segments_removed = 0
        segments_kept = 0

        fm = (open(args.matched_output, "w", encoding="utf-8", errors="replace")
              if args.matched_output else None)
        try:
            with open(args.output, "w", encoding="utf-8", errors="replace") as fout:
                first_out = True
                first_m = True

                def write_seg(fh, seg, first_flag):
                    if first_flag[0]:
                        fh.write(seg)
                        first_flag[0] = False
                    else:
                        fh.write(" " + seg)

                def consume(k, r):
                    nonlocal segments_removed, first_m, pending_text
                    if fm is not None:  # --matched-output opsiyonel: None olabilir
                        for seg in pending[:k]:
                            write_seg(fm, seg, [first_m])
                            first_m = False
                    r["count"] += 1
                    segments_removed += k
                    cut = sum(len(x) for x in pending[:k]) + k  # segmentler + ayraç boşlukları
                    pending_text = pending_text[cut:]
                    del pending[:k]

                def survivor(seg):
                    nonlocal first_out, segments_kept, pending_text
                    write_seg(fout, seg, [first_out])
                    first_out = False
                    segments_kept += 1
                    pending_text = (pending_text[len(seg) + 1:]
                                    if len(pending_text) > len(seg) else "")

                pending.append(first_seg)
                pending_text = first_seg
                segments_in = 1
                chars_read = len(first_seg)
                last_progress = time.monotonic()
                for seg in seg_iter:
                    segments_in += 1
                    chars_read += len(seg) + 1
                    pending.append(seg)
                    pending_text = pending_text + " " + seg
                    while pending:
                        r, k = try_head()
                        if r is not None:
                            consume(k, r)
                            continue
                        if len(pending) >= max_span:
                            # hiçbir kural başı kapamadı ve daha fazla uzayamaz
                            survivor(pending.pop(0))
                            continue
                        break  # yeni segment gelmesini bekle
                    now_t = time.monotonic()
                    if now_t - last_progress >= args.progress_every_sec:
                        last_progress = now_t
                        elapsed = now_t - started
                        log.info("İŞLENİYOR: %d segment (%.2f M karakter) | elenen %d | "
                                 "kalan %d | bekleyen %d | %.0f seg/sn",
                                 segments_in, chars_read / 1e6, segments_removed,
                                 segments_kept, len(pending),
                                 segments_in / elapsed if elapsed > 0 else 0)
                        payload["segments_read"] = segments_in
                        payload["segments_removed"] = segments_removed
                        payload["segments_kept"] = segments_kept
                        payload["chars_read"] = chars_read
                        flush("running")
                    elif segments_in % args.report_every == 0:  # K8
                        payload["segments_read"] = segments_in
                        payload["segments_removed"] = segments_removed
                        payload["segments_kept"] = segments_kept
                        payload["chars_read"] = chars_read
                        flush("running")
                # EOF: artık uzama yok, kalan pending tamamen karara bağlanır
                while pending:
                    r, k = try_head()
                    if r is not None:
                        consume(k, r)
                        continue
                    survivor(pending.pop(0))
                if segments_kept > 0:
                    fout.write("\n")
                if fm is not None and segments_removed > 0:
                    fm.write("\n")
        finally:
            if fm is not None:
                fm.close()

        # --- koruma denklemi: kanıtlanmalı -----------------------------------
        conservation_ok = (segments_in == segments_removed + segments_kept)
        coverage = (100.0 * segments_removed / segments_in) if segments_in else 0.0

        # --- özet (readable sıklık raporu) -----------------------------------
        patterns_summary = sorted(
            ({
                "tag": r["tag"],
                "label": r["label"],
                "readable": r["readable"],
                "matched": r["count"],
                "seen_claimed": r["seen_claimed"],
                "segments": r["span"] if not r["unbounded"] else ("≤%d" % r["span"]),
                "over_broad": (segments_in > 0
                               and r["count"] / segments_in > args.max_rule_coverage),
            } for r in rules),
            key=lambda d: -d["matched"])
        overview = ["×%-6d [%s] %s" % (p["matched"], p["label"] or "-", p["readable"])
                    for p in patterns_summary if p["matched"] > 0]
        for p in patterns_summary:
            if p["over_broad"]:
                log.warning("aşırı-geniş kural işaretlendi: %s (%%%.1f > %%%.0f eşiği) — "
                            "--exclude-rules %s ile dışlayıp yeniden koşabilirsiniz",
                            p["tag"], 100.0 * p["matched"] / segments_in,
                            100.0 * args.max_rule_coverage, p["tag"])

        with open(args.summary, "w", encoding="utf-8") as f:
            json.dump({
                "script": "clean_patterns_sum_and_list_new.py",
                "generated_at_utc": _utcnow(),
                "input": os.path.abspath(args.input),
                "rules_files": [os.path.abspath(p) for p in args.rules],
                "totals": {
                    "segments_in": segments_in,
                    "segments_removed": segments_removed,
                    "segments_kept": segments_kept,
                    "coverage_pct": round(coverage, 2),
                    "conservation_ok": conservation_ok,
                },
                "patterns": patterns_summary,
                "overview_lines": overview,
                "skipped_rules": skipped,
            }, f, indent=2, ensure_ascii=False)
            f.write("\n")

        payload.update({
            "segments_read": segments_in,
            "segments_removed": segments_removed,
            "segments_kept": segments_kept,
            "coverage_pct": round(coverage, 2),
            "conservation_ok": conservation_ok,
            "summary": os.path.abspath(args.summary),
        })
        if not conservation_ok:
            payload["error"] = ("KORUMA DENKLEMİ BOZULDU: in=%d != removed=%d + kept=%d "
                                "— segment kaybı var, çıktılara güvenmeyin"
                                % (segments_in, segments_removed, segments_kept))
            flush("failed")
            log.error("hata: %s", payload["error"])
            return 1
        flush("completed")
        log.info("bitti: %d segment (elenen %d, kalan %d, kapsam %%%.1f), %d/%d kural "
                 "eşleşti, %.1f sn — özet: %s",
                 segments_in, segments_removed, segments_kept, coverage,
                 sum(1 for r in rules if r["count"] > 0), len(rules),
                 payload["elapsed_sec"], args.summary)
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


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="clean_patterns_sum_and_list_new.py",
        description="LLM kalıplarıyla eşleşenleri eler (çoklu-segment olaylar atomik), "
                    "readable sıklık özetini JSON'a yazar, eşleşmeyenleri sanitize "
                    "formatında sonraki tura bırakır. Koruma denklemi her koşuda "
                    "kanıtlanır: in = removed + kept.")
    p.add_argument("--input", required=True,
                   help="girdi: sanitize.py çıktısı (tek satır _SOL_/_EOL_)")
    p.add_argument("--rules", required=True, action="append",
                   help="LLM kalıp JSON'u; birden çok --rules = birikimli baseline")
    p.add_argument("--output", required=True,
                   help="eşleşmeyen segmentler (sanitize formatı — chunk.py'a beslenir)")
    p.add_argument("--summary", default="summary.json",
                   help="readable sıklık özeti JSON'u (varsayılan: summary.json)")
    p.add_argument("--matched-output", default=None,
                   help="isteğe bağlı: elenen segmentler ayrı dosyaya (denetim)")
    p.add_argument("--only-rules", default=None,
                   help="yalnız bu tag'ler (virgülle)")
    p.add_argument("--exclude-rules", default=None,
                   help="bu tag'leri dışla (virgülle)")
    p.add_argument("--max-rule-coverage", type=float, default=0.4,
                   help="tek kuralın girdinin bu oranından fazlasını yutması "
                        "aşırı-genişlik flag'i (varsayılan: 0.40)")
    p.add_argument("--max-event-lines", type=int, default=500,
                   help="(...)+ gibi sınırsız tekrarlı kuralların olay satırı tavanı "
                        "(varsayılan: 500)")
    p.add_argument("--report", default="report.json",
                   help="çalışma raporu dosyası (varsayılan: report.json)")
    p.add_argument("--report-every", type=int, default=50000,
                   help="report.json güncelleme aralığı, segment (varsayılan: 50000)")
    p.add_argument("--progress-every-sec", type=float, default=5.0,
                   help="canlı ilerleme logu aralığı, saniye — segment/karakter/elenen/"
                        "kalan/hız gösterir (varsayılan: 5.0; 0 = kapalı)")
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
