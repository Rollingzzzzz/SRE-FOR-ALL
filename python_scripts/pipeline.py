#!/usr/bin/env python3
"""Öğrenme döngüsü orkestratörü — python_scripts/pipeline.py (K1-K8 uyumlu).

Ne yapar (bizim elle yaptığımızın birebir otomatiği):
  original log  ->  RAUNT döngüsü (varsayılan en fazla N raunt):
    [0] sanitize (girdi zaten sanitize çıktısıysa atlanır)
    [1] chunk     : kalanlardan --tokens bütçeli dilim
    [2] llm       : dilimin kalıplarını öğrenir (SSE canlı akış ekrana düşer)
    [3] clean     : kalanlardan eşleşenleri eler -> yeni kalanlar
  ...ta ki: kapsam kazancı durur / raunt sınırı / öğrenilecek kural kalmaz /
  kırmızı bayrak (herhangi bir adım rc!=0).

Klasör düzeni (her raunt kendi klasöründe, alt uygulamaların TÜM çıktıları orada):
  <workdir>/round_000/clean.log + report_sanitize.json
  <workdir>/round_001/chunk.txt + report_chunk.json
                 patterns.json + report_llm.json + llm/*_request|response.json
                 unmatched.log + summary.json + removed.log + report_clean.json
  <workdir>/pipeline_report.json   (K8: raunt raunt canlı güncellenir, atomik)

Ekranda: her adımın başlığı + alt uygulamanın kendi canlı satırları (akan düşünme,
İŞLENİYOR...) + raunt kapanış tablosu — kullanıcı boş bakmaz (K4, stderr).

Dur kuralları (tümü parametrik):
  --max-rounds          : raunt tavanı (varsayılan 3)
  --min-round-gain-pct  : bir raunt kapsamı bu kadar puandan az büyütürse dur
                          (varsayılan 1.0 = %1 puan; azalan getiri tanımı)
  kırmızı bayrak        : alt adım beklenmeyen rc!=0 -> anında dur + rapora sebeb
  iyi son               : kalan 0, veya LLM'in kurallarının hiçbirinin kalanlarla
                          eşleşmemesi/geçerli kural kalmaması -> "öğrenildi kadarı"

Örnek:
  python3 python_scripts/pipeline.py \
    --input example_logs/original/hadoop.log \
    --workdir data/pipeline_deneme1 \
    --max-rounds 3
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

# alt modüller K3 gereği import edilebilir (yan etkisiz, main(args)/run(args))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sanitize as m_sanitize          # noqa: E402
import chunk as m_chunk                # noqa: E402
import call_llm_for_pattern_recon as m_llm   # noqa: E402
import clean_patterns_sum_and_list_new as m_clean  # noqa: E402

log = logging.getLogger("pipeline")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_report(path: str, payload: dict) -> None:
    """report.json'u atomik yazar (K8)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def banner(msg: str, *args) -> None:
    log.info("")
    log.info("=" * 72)
    log.info(msg, *args)
    log.info("=" * 72)


def run_step(mod, argv, title) -> int:
    """Alt modülü kendi parse_args+run'sı ile koşar; rc döner. Alt modülün
    canlı logları (akan:/İŞLENİYOR:) aynı stderr'e zaten akar."""
    started = time.monotonic()
    args = mod.parse_args(argv)
    rc = mod.run(args)
    log.info("[%s] bitti: rc=%d, %.1f sn", title, rc, time.monotonic() - started)
    return rc


def read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def looks_sanitized(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(64)
        return head.lstrip().startswith("_SOL_")
    except OSError:
        return False


def run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    os.makedirs(args.workdir, exist_ok=True)

    payload = {
        "script": "pipeline.py",
        "status": "running",
        "input": os.path.abspath(args.input),
        "workdir": os.path.abspath(args.workdir),
        "started_at_utc": _utcnow(),
        "updated_at_utc": _utcnow(),
        "elapsed_sec": 0.0,
        "rounds_done": 0,
        "rounds": [],
        "total_segments": None,
        "total_removed": 0,
        "coverage_pct": 0.0,
        "llm_totals": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                       "reasoning_tokens": 0},
        "stop_reason": "",
    }

    def flush(status: str) -> None:
        payload["status"] = status
        payload["updated_at_utc"] = _utcnow()
        payload["elapsed_sec"] = round(time.monotonic() - started, 1)
        try:
            write_report(os.path.join(args.workdir, "pipeline_report.json"), payload)
        except OSError as exc:
            log.warning("pipeline_report.json yazılamadı: %s", exc)

    def hard_fail(reason: str) -> int:
        payload["stop_reason"] = reason
        banner("DURDURULDU (kırmızı bayrak): %s" % reason)
        flush("failed")
        return 1

    try:
        banner("PIPELINE BAŞLADI | girdi: %s | workdir: %s | max %d raunt",
               args.input, args.workdir, args.max_rounds)

        # --- [0] sanitize (girdi zaten sanitize çıktısıysa atla) ------------
        if looks_sanitized(args.input):
            current = os.path.abspath(args.input)
            log.info("[sanitize] ATLANDI — girdi zaten sanitize çıktısı: %s", current)
        else:
            rdir = os.path.join(args.workdir, "round_000")
            os.makedirs(rdir, exist_ok=True)
            clean_path = os.path.join(rdir, "clean.log")
            rc = run_step(m_sanitize, [
                "--input", args.input,
                "--output", clean_path,
                "--report", os.path.join(rdir, "report_sanitize.json"),
                "--report-every", str(args.report_every),
            ] + (["--debug"] if args.debug else []), "sanitize")
            if rc != 0:
                return hard_fail("sanitize rc=%d" % rc)
            current = clean_path

        total = None
        prev_coverage = 0.0

        # --- raunt döngüsü ----------------------------------------------------
        for rnd in range(1, args.max_rounds + 1):
            rdir = os.path.join(args.workdir, "round_%03d" % rnd)
            os.makedirs(rdir, exist_ok=True)
            banner("RAUNT %d/%d — klasör: %s" % (rnd, args.max_rounds, rdir))
            round_rec = {"round": rnd, "dir": rdir}

            # [1] chunk
            chunk_path = os.path.join(rdir, "chunk.txt")
            rc = run_step(m_chunk, [
                "--input", current,
                "--output", chunk_path,
                "--tokens", str(args.tokens),
                "--report", os.path.join(rdir, "report_chunk.json"),
            ] + (["--debug"] if args.debug else []), "1/3 chunk")
            if rc != 0:
                return hard_fail("raunt %d chunk rc=%d" % (rnd, rc))
            payload["current_step"] = "llm"          # adım adım canlı (K8)
            payload["current_round"] = rnd
            flush("running")
            chunk_info = read_json(os.path.join(rdir, "report_chunk.json"))
            round_rec["chunk_segments"] = chunk_info.get("lines_selected")
            log.info("        dilim: %s segment", chunk_info.get("lines_selected"))

            # [2] llm (pahalı adım — canlı akış alt modülden gelir)
            patterns_path = os.path.join(rdir, "patterns.json")
            llm_argv = [
                "--input", chunk_path,
                "--output", patterns_path,
                "--llm-dir", os.path.join(rdir, "llm"),
                "--report", os.path.join(rdir, "report_llm.json"),
                "--timeout", str(args.llm_timeout),
                "--stream-idle-timeout", str(args.llm_stream_idle_timeout),
            ]
            if args.llm_model:
                llm_argv += ["--model", args.llm_model]
            if args.llm_base_url:
                llm_argv += ["--base-url", args.llm_base_url]
            if args.no_stream:
                llm_argv += ["--no-stream"]
            if args.debug:
                llm_argv += ["--debug"]
            rc = run_step(m_llm, llm_argv, "2/3 llm")
            llm_info = read_json(os.path.join(rdir, "report_llm.json"))
            usage = llm_info.get("usage") or {}
            det = usage.get("completion_tokens_details") or {}
            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                payload["llm_totals"][k] += usage.get(k) or 0
            payload["llm_totals"]["reasoning_tokens"] += det.get("reasoning_tokens") or 0
            round_rec.update({
                "llm_ok": llm_info.get("json_valid"),
                "patterns": llm_info.get("pattern_count"),
                "llm_usage": usage,
            })
            log.info("        kalıp: %s | json_valid: %s | token: %s (toplam: %s)",
                     llm_info.get("pattern_count"), llm_info.get("json_valid"),
                     usage.get("total_tokens"), payload["llm_totals"]["total_tokens"])
            if rc != 0:
                # json sözleşme ihlali dahi kırmızı bayrak: sessiz tur atlanmaz
                return hard_fail("raunt %d llm rc=%d (%s)" % (rnd, rc, llm_info.get("error", "")))
            payload["current_step"] = "clean"
            flush("running")

            # [3] clean
            unmatched_path = os.path.join(rdir, "unmatched.log")
            rc = run_step(m_clean, [
                "--input", current,
                "--rules", patterns_path,
                "--output", unmatched_path,
                "--summary", os.path.join(rdir, "summary.json"),
                "--matched-output", os.path.join(rdir, "removed.log"),
                "--report", os.path.join(rdir, "report_clean.json"),
                "--progress-every-sec", "10",
            ] + (["--debug"] if args.debug else []), "3/3 clean")
            clean_info = read_json(os.path.join(rdir, "summary.json"))
            totals = clean_info.get("totals") or {}
            if rc != 0:
                err = str((read_json(os.path.join(rdir, "report_clean.json")) or {})
                          .get("error", ""))
                if "geçerli kural" in err:
                    payload["stop_reason"] = ("raunt %d: öğrenilecek geçerli kural kalmadı "
                                              "(kalanlar artık kalıp vermiyor)" % rnd)
                    payload["rounds_done"] = rnd
                    payload["rounds"].append(round_rec)
                    break
                return hard_fail("raunt %d clean rc=%d (%s)" % (rnd, rc, err))

            kept = totals.get("segments_kept", 0)
            removed_this = totals.get("segments_removed", 0)
            if total is None:
                total = totals.get("segments_in") or 0
                payload["total_segments"] = total
            coverage = (100.0 * (total - kept) / total) if total else 0.0
            gain = coverage - prev_coverage
            round_rec.update({
                "segments_in": totals.get("segments_in"),
                "removed": removed_this,
                "kept": kept,
                "coverage_pct": round(coverage, 2),
                "gain_pct": round(gain, 2),
            })
            payload["rounds_done"] = rnd
            payload["rounds"].append(round_rec)
            payload["total_removed"] = total - kept
            payload["coverage_pct"] = round(coverage, 2)
            prev_coverage = coverage
            payload["current_step"] = "chunk"        # sıradaki raunt
            payload["current_round"] = rnd + 1
            flush("running")

            log.info("")
            log.info("RAUNT %d SONUCU | elenen %s | kalan %s | kapsam %%%.1f "
                     "(bu raunt +%.1f puan)", rnd, f"{removed_this:,}", f"{kept:,}",
                     coverage, gain)

            # --- dur koşulları (bir sonraki rauntu başlatmadan) --------------
            if kept == 0:
                payload["stop_reason"] = "kalan segment kalmadı — her şey öğrenildi/e_lendi"
                break
            if rnd < args.max_rounds and gain < args.min_round_gain_pct:
                payload["stop_reason"] = ("raunt %d kazancı +%.2f puan < eşiği %.1f — "
                                          "azalan getiri, duruyoruz" % (rnd, gain,
                                                                       args.min_round_gain_pct))
                break
            current = unmatched_path

        else:
            payload["stop_reason"] = "raunt tavanına ulaşıldı (--max-rounds %d)" % args.max_rounds

        if not payload["stop_reason"]:
            payload["stop_reason"] = "tamamlandı"

        # --- kapanış tablosu --------------------------------------------------
        banner("PIPELINE BİTTİ — %s" % payload["stop_reason"])
        log.info("raunt | elenen      | kalan       | kapsam | kazanç")
        for r in payload["rounds"]:
            if "coverage_pct" in r:
                log.info("  %2d | %10s | %10s | %5.1f%% | +%5.2f",
                         r["round"], f"{r.get('removed', 0):,}",
                         f"{r.get('kept', 0):,}", r["coverage_pct"], r["gain_pct"])
        log.info("TOPLAM: %s/%s segment elendi (%%%s) | LLM: %s token "
                 "(düşünme %s) | süre %.1f sn",
                 f"{payload['total_removed']:,}", f"{payload.get('total_segments') or 0:,}",
                 payload["coverage_pct"], f"{payload['llm_totals']['total_tokens']:,}",
                 f"{payload['llm_totals']['reasoning_tokens']:,}",
                 time.monotonic() - started)
        flush("completed")
        return 0
    except KeyboardInterrupt:
        flush("interrupted")
        log.error("kullanıcı tarafından kesildi (tamamlanan rauntlar raporda)")
        return 130
    except Exception as exc:  # K3: traceback yok, temiz mesaj
        payload["error"] = "%s: %s" % (type(exc).__name__, exc)
        flush("failed")
        log.error("hata: %s", payload["error"])
        return 1


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
        prog="pipeline.py",
        description="Öğrenme döngüsü orkestratörü: sanitize -> (chunk -> LLM -> clean) x N. "
                    "Her raunt kendi klasörüne yazar; alt adımların canlı çıktıları ekrana akar; "
                    "kırmızı bayrakta anında durur.")
    p.add_argument("--input", required=True,
                   help="orijinal ham log (veya sanitize çıktısı — otomatik algılanır)")
    p.add_argument("--workdir", default=None,
                   help="çalışma kökü (varsayılan: data/pipeline_<zaman damgası>)")
    p.add_argument("--max-rounds", type=nonnegative_int, default=3,
                   help="en fazla raunt sayısı (varsayılan: 3)")
    p.add_argument("--tokens", type=int, default=10000,
                   help="her rauntta LLM'e gidecek chunk token bütçesi (varsayılan: 10000)")
    p.add_argument("--min-round-gain-pct", type=float, default=1.0,
                   help="bir raunt kapsamı bu kadar PUANDAN az büyütürse dur (varsayılan: 1.0)")
    p.add_argument("--llm-model", default=None, help="LLM model adı (zai.env varsayılanı)")
    p.add_argument("--llm-base-url", default=None,
                   help="LLM base URL (zai.env varsayılanı; test için mock URL verilebilir)")
    p.add_argument("--llm-timeout", type=float, default=120.0,
                   help="LLM ağ timeout (stream kapalıyken), sn (varsayılan: 120)")
    p.add_argument("--llm-stream-idle-timeout", type=float, default=300.0,
                   help="stream'de tam sessizlik iptali, sn (varsayılan: 300)")
    p.add_argument("--no-stream", action="store_true", help="LLM SSE akışını kapat")
    p.add_argument("--report-every", type=int, default=50000,
                   help="sanitize report aralığı, satır (varsayılan: 50000)")
    p.add_argument("--report", default="report_pipeline.json",
                   help="pipeline raporu (aynı zamanda <workdir>/pipeline_report.json)")
    p.add_argument("--debug", action="store_true", help="tüm alt adımlar ayrıntılı loglar")
    return p.parse_args(argv)


def main(argv=None) -> None:
    args = parse_args(argv)
    if args.workdir is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        args.workdir = os.path.join("data", "pipeline_%s" % stamp)
    os.makedirs(args.workdir, exist_ok=True)
    # K4: loglar stderr'de + workdir/console.log'a (GUI canlı konsolu — elle
    # başlatılan koşularda bile dosyaya düşer)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    sh = logging.StreamHandler(sys.stderr)
    fh = logging.FileHandler(os.path.join(args.workdir, "console.log"), encoding="utf-8")
    for h in (sh, fh):
        h.setFormatter(fmt)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO, handlers=[sh, fh])
    sys.exit(run(args))


if __name__ == "__main__":
    main()
